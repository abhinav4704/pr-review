"""Review findings: data model, JSON parsing, severity helpers, dedupe.

Self-contained — no dependency on any other review code.

Severity meaning (project convention):
    critical  -> something breaks (caller errors / crash), or another serious issue the
                 model is confident about. The model's "critical" is trusted, with one
                 light guard: it must be backed by evidence (the relevant line shown);
                 an unsupported "critical" is trimmed to high.
    high      -> security vulnerability, exposed secret/key, or a serious bug
    medium    -> performance / unoptimized
    low       -> nice-to-have improvement
    info      -> purely informational

`cap_finding_severity` (applied in `parse_findings`) applies the light guard to
every finding the system produces.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import List, Optional

SEVERITY_WEIGHT = {"critical": 10, "high": 6, "medium": 3, "low": 1, "info": 0}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
SEV_BADGE = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    "info": "⚪",
}

# The output contract we ask every pass to follow.
FINDINGS_SCHEMA = """You are reviewing a pull request. You see the CHANGED lines (the diff,
or the lines marked >>>) plus the surrounding code for context. Only report problems the
change ADDS or BREAKS. Use the context to judge, but do not report old problems that have
nothing to do with this change. If the change looks fine, return [].

Reply with ONLY a JSON array (no extra text, no markdown). Each item looks like this:
{
  "kind": "issue" | "suggestion",
  "category": "breaking" | "bug" | "vulnerability" | "optimization" | "suggestion",
  "severity": "critical" | "high" | "medium" | "low" | "info",
  "file": string,            // file path
  "line": integer,           // the most relevant line (0 if you are not sure)
  "title": string,           // one short sentence
  "explanation": string,     // 2-4 sentences: what is wrong and why it matters
  "evidence": string,        // a short code snippet showing the problem
  "recommendation": string   // how to fix it
}

How to choose category and severity:
- breaking: the change breaks callers or crashes at runtime. severity: critical. Put the
  exact broken line of code in "evidence" — a critical with no evidence is trimmed to high.
- bug: the new code is wrong or uses something the wrong way. severity: critical or high.
- vulnerability: the change exposes a secret, key, or token (critical), or leaves a security
  hole open like SQL injection, unchecked input, or missing auth (high, or medium if low risk).
- optimization: the change is slow or wasteful. severity: medium.
- suggestion: a nice-to-have improvement. kind "suggestion", severity low.
If you find nothing, return []."""


@dataclass
class Finding:
    category: str
    severity: str
    file: str
    line: int
    title: str
    explanation: str
    evidence: str
    recommendation: str
    kind: str = "issue"
    prompt_id: str = ""
    changed_file: str = ""
    changed_function: str = ""
    dependent_function: str = ""
    dependent_file: str = ""
    dependent_line: int = 0
    callsite_file: str = ""
    callsite_line: int = 0
    chain_line_non_llm: str = ""
    chain_source: str = ""
    provenance_status: str = ""
    impact_reason: str = ""
    source_fix_example: str = ""
    dependent_fix_example: str = ""

    @classmethod
    def from_dict(cls, d: dict, default_file: str = "") -> Optional["Finding"]:
        try:
            severity = str(d.get("severity", "low")).lower()
            if severity not in SEVERITY_WEIGHT:
                severity = "low"
            kind = str(d.get("kind", "")).lower()
            if kind not in ("issue", "suggestion"):
                kind = "suggestion" if severity == "info" else "issue"
            title = str(d.get("title", "")).strip()
            if not title:
                return None
            return cls(
                category=str(d.get("category", "code-quality")),
                severity=severity,
                file=str(d.get("file") or default_file),
                line=int(d.get("line", 0) or 0),
                title=title,
                explanation=str(d.get("explanation", "")).strip(),
                evidence=str(d.get("evidence", "")).strip(),
                recommendation=str(d.get("recommendation", "")).strip(),
                kind=kind,
                prompt_id=str(d.get("prompt_id", "")).strip(),
                changed_file=str(d.get("changed_file", "")).strip(),
                changed_function=str(d.get("changed_function", "")).strip(),
                dependent_function=str(d.get("dependent_function", "")).strip(),
                dependent_file=str(d.get("dependent_file", "")).strip(),
                dependent_line=int(d.get("dependent_line", 0) or 0),
                callsite_file=str(d.get("callsite_file", "")).strip(),
                callsite_line=int(d.get("callsite_line", 0) or 0),
                chain_line_non_llm=str(d.get("chain_line_non_llm", "")).strip(),
                chain_source=str(d.get("chain_source", "")).strip(),
                provenance_status=str(d.get("provenance_status", "")).strip(),
                impact_reason=str(d.get("impact_reason", "")).strip(),
                source_fix_example=str(d.get("source_fix_example", "")).strip(),
                dependent_fix_example=str(d.get("dependent_fix_example", "")).strip(),
            )
        except (TypeError, ValueError):
            return None


def _extract_json_array(text: str):
    """Best-effort extraction of a JSON array from an LLM response."""
    if not text:
        return []
    # strip markdown fences if present
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    # direct parse
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        pass
    # fall back: grab the outermost [...] block
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            return []
    return []


def cap_finding_severity(f: Finding) -> Finding:
    """Light guard on the model's "critical": keep it, unless it is unsupported.

    The model's severity is trusted across categories; the only trim is when a
    "critical" comes with no evidence to back it — that drops to high. Never upgrades.
    """
    if f.severity == "critical" and not (f.evidence or "").strip():
        f.severity = "high"
    return f


def parse_findings(text: str, default_file: str = "") -> List[Finding]:
    out: List[Finding] = []
    for item in _extract_json_array(text):
        if isinstance(item, dict):
            f = Finding.from_dict(item, default_file=default_file)
            if f:
                out.append(cap_finding_severity(f))
    return out


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", t.lower()).strip()


def dedupe(findings: List[Finding]) -> List[Finding]:
    """Drop near-duplicate findings, keeping the highest-severity copy.

    Two findings collide when they share file, a ~5-line proximity bucket, and a
    normalized title.
    """
    best: dict = {}
    for f in findings:
        key = (
            f.file,
            f.line // 5,
            _norm_title(f.title),
            (f.changed_function or "").strip().lower(),
            (f.prompt_id or "").strip().lower(),
        )
        cur = best.get(key)
        if cur is None or SEVERITY_WEIGHT.get(f.severity, 0) > SEVERITY_WEIGHT.get(cur.severity, 0):
            best[key] = f
    return list(best.values())


def sort_by_severity(findings: List[Finding]) -> List[Finding]:
    return sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))


def severity_counts(findings: List[Finding]) -> dict:
    counts = {s: 0 for s in SEVERITY_WEIGHT}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts
