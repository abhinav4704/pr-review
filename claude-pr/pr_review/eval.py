"""Golden evaluation harness.

Measures the system's precision, recall, and noise rate against a labelled
set of PRs where the expected findings are known.

Usage:
    from pr_review.eval import EvalCase, EvalHarness, run_eval

    cases = [
        EvalCase(
            name="sql_injection",
            diff_text=open("testdata/sqli.diff").read(),
            repo_path="testdata/sqli_repo",
            expected_findings=[
                ExpectedFinding(
                    file="app/db.py",
                    line_range=(10, 20),
                    category="security",
                    min_severity="high",
                    description="SQL injection via f-string",
                ),
            ],
        ),
        ...
    ]
    results = run_eval(cases, nova_client, token_budget=8000)
    print(results.summary())

The LLM judge compares each actual finding against the expected set and
decides if it's a true positive, false positive, or if an expected finding
was missed (false negative).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from .blast import blast_radius
from .diff import map_changes, parse_diff
from .graph import build_graph
from .llm import NovaClient
from .review import OverallResult, run_review

if TYPE_CHECKING:
    from .embeddings import EmbeddingIndex

JUDGE_SYSTEM = (
    "You are an expert software engineer evaluating AI-generated code review findings. "
    "You decide whether an actual finding matches an expected finding (true positive), "
    "is completely wrong or hallucinated (false positive), or if an expected finding "
    "was missed (false negative). Be strict: a finding matches only if it correctly "
    "identifies the actual problem in the actual file/location."
)

JUDGE_PROMPT = """Given the expected finding and actual findings, classify each.

Expected finding:
{expected}

Actual findings from the system (may be empty):
{actual}

Return JSON:
{{
  "matched": true | false,
  "matched_index": int | null,   // index of the matching actual finding, or null
  "reason": "one sentence"
}}
"""


@dataclass
class ExpectedFinding:
    file: str
    line_range: Tuple[int, int]   # (start, end) inclusive
    category: str                  # e.g. "security", "bug/regression"
    min_severity: str              # minimum expected severity
    description: str               # what the issue is (used by the judge)


@dataclass
class EvalCase:
    name: str
    diff_text: str
    repo_path: str                  # path to the repo on disk at PR head
    expected_findings: List[ExpectedFinding] = field(default_factory=list)
    notes: str = ""


@dataclass
class EvalFinding:
    expected: ExpectedFinding
    matched: bool = False
    matched_actual_index: Optional[int] = None
    judge_reason: str = ""


@dataclass
class CaseResult:
    case_name: str
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    total_expected: int = 0
    total_actual: int = 0
    details: List[EvalFinding] = field(default_factory=list)
    latency_s: float = 0.0
    dossier_tokens: int = 0

    @property
    def precision(self) -> float:
        if self.total_actual == 0:
            return 1.0
        return self.true_positives / self.total_actual

    @property
    def recall(self) -> float:
        if self.total_expected == 0:
            return 1.0
        return self.true_positives / self.total_expected

    @property
    def noise_rate(self) -> float:
        if self.total_actual == 0:
            return 0.0
        return self.false_positives / self.total_actual

    def summary_line(self) -> str:
        return (
            f"{self.case_name}: "
            f"TP={self.true_positives} FP={self.false_positives} FN={self.false_negatives} | "
            f"precision={self.precision:.2f} recall={self.recall:.2f} "
            f"noise={self.noise_rate:.2f} | "
            f"{self.latency_s:.1f}s ~{self.dossier_tokens} tokens"
        )


@dataclass
class EvalResults:
    case_results: List[CaseResult] = field(default_factory=list)

    @property
    def macro_precision(self) -> float:
        if not self.case_results:
            return 0.0
        return sum(r.precision for r in self.case_results) / len(self.case_results)

    @property
    def macro_recall(self) -> float:
        if not self.case_results:
            return 0.0
        return sum(r.recall for r in self.case_results) / len(self.case_results)

    @property
    def macro_noise(self) -> float:
        if not self.case_results:
            return 0.0
        return sum(r.noise_rate for r in self.case_results) / len(self.case_results)

    def summary(self) -> str:
        lines = ["=== Evaluation Results ===", ""]
        for r in self.case_results:
            lines.append(r.summary_line())
        lines.append("")
        lines.append(f"Macro precision : {self.macro_precision:.3f}")
        lines.append(f"Macro recall    : {self.macro_recall:.3f}")
        lines.append(f"Macro noise     : {self.macro_noise:.3f}")
        return "\n".join(lines)


def _judge(
    expected: ExpectedFinding,
    actuals: List[Dict],
    nova: NovaClient,
) -> Tuple[bool, Optional[int], str]:
    """Use the LLM judge to decide if the expected finding was correctly identified."""
    exp_str = json.dumps({
        "file": expected.file,
        "line_range": list(expected.line_range),
        "category": expected.category,
        "min_severity": expected.min_severity,
        "description": expected.description,
    }, indent=2)
    act_str = json.dumps(actuals, indent=2) if actuals else "[]"

    raw = nova.complete_json(
        JUDGE_SYSTEM,
        JUDGE_PROMPT.format(expected=exp_str, actual=act_str),
    )
    if not isinstance(raw, dict):
        return False, None, "judge parse error"
    matched = bool(raw.get("matched", False))
    idx = raw.get("matched_index")
    if idx is not None:
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = None
    return matched, idx, str(raw.get("reason", ""))


def run_eval(
    cases: List[EvalCase],
    nova: NovaClient,
    embed_index: Optional["EmbeddingIndex"] = None,
    token_budget: int = 8000,
    verify: bool = True,
    agents: list = None,
) -> EvalResults:
    results = EvalResults()

    for case in cases:
        t0 = time.time()
        # build graph + run review
        cg = build_graph(case.repo_path)
        file_diffs = parse_diff(case.diff_text)
        changes = map_changes(cg, file_diffs)
        blast = blast_radius(cg, changes)
        overall: OverallResult = run_review(
            cg=cg,
            changes=changes,
            blast=blast,
            diff_text=case.diff_text,
            file_diffs=file_diffs,
            nova=nova,
            embed_index=embed_index,
            token_budget=token_budget,
            verify=verify,
            agents=agents,
        )
        latency = time.time() - t0

        # flatten actual findings to plain dicts for the judge
        actual_dicts = [
            {"index": i, "category": f.category, "file": f.file, "line": f.line,
             "severity": f.severity, "title": f.title, "explanation": f.explanation}
            for i, f in enumerate(overall.all_findings)
        ]

        case_result = CaseResult(
            case_name=case.name,
            total_expected=len(case.expected_findings),
            total_actual=len(overall.all_findings),
            latency_s=latency,
            dossier_tokens=overall.dossier_tokens,
        )

        matched_actual_indices = set()

        for expected in case.expected_findings:
            # filter actual findings to the same file for efficiency
            file_actuals = [d for d in actual_dicts if d["file"] == expected.file]
            matched, idx, reason = _judge(expected, file_actuals, nova)
            detail = EvalFinding(
                expected=expected,
                matched=matched,
                matched_actual_index=idx,
                judge_reason=reason,
            )
            case_result.details.append(detail)
            if matched:
                case_result.true_positives += 1
                if idx is not None:
                    matched_actual_indices.add(idx)
            else:
                case_result.false_negatives += 1

        # false positives: actual findings not matched by any expected
        case_result.false_positives = (
            len(overall.all_findings) - len(matched_actual_indices)
        )

        results.case_results.append(case_result)

    return results
