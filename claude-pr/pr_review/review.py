"""Per-file specialist agent orchestration — two-pass review.

Pass 1 — whole-file sweep:
    1. Split the WHOLE file into definition-aware chunks  (context.make_file_chunks)
    2. Render each chunk (full source, changed lines marked '+')
    3. Run whole-file agents (security, architecture, performance)

Pass 2 — changed-function + dependency sweep:
    4. Group added lines into proximity chunks             (context.make_chunks)
    5. Build rich dossiers: changed fn + callers/callees/tests (context.build_chunk_dossier)
    6. Run dependency-aware agents (security, correctness, api_contract, architecture)

Both passes feed into:
    7. Merge all candidates, deduplicate by (file, line, title)
    8. Run evidence verifier over the combined dossier
    9. Cross-file breaking-change pass over changed (+) functions only
"""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

from .agents import (
    ALL_AGENTS,
    DEPENDENCY_INSTRUCTIONS,
    DEPENDENCY_SYSTEM,
    FINDINGS_SCHEMA,
    Finding,
    _parse_findings,
)
from .blast import BlastResult
from .context import (
    Chunk,
    _slice_node_capped,
    build_all_chunk_dossiers,
    build_all_file_chunk_dossiers,
    make_chunks,
    make_file_chunks,
)
from .diff import (
    ChangedNode,
    FileDiff,
    group_by_file,
    node_was_modified,
    old_signature_for,
)
from .filters import should_review
from .graph import CodeGraph
from .identity import IdentityCard, build_identity_card, render_card
from .llm import NovaClient
from .profiles import DEEP, ReviewProfile, build_agents

if TYPE_CHECKING:
    from .embeddings import EmbeddingIndex

_SEVERITY_WEIGHT = {"critical": 10, "high": 6, "medium": 3, "low": 1, "info": 0}

VERIFY_SYSTEM = (
    "You are a strict reviewer of AI-generated code findings. For each finding "
    "decide whether the cited code ACTUALLY exhibits the claimed problem. "
    "Reject anything not grounded in the provided code. Reject if a guard, "
    "type constraint, or existing test already handles it. Be skeptical."
)

VERIFY_INSTRUCTIONS = """Review context and candidate findings below.

Return ONLY a JSON array:
[{{"index": int, "valid": true|false, "reason": "one sentence"}}, ...]

=== CONTEXT ===
{dossier}

=== CANDIDATE FINDINGS ===
{findings}
"""


def _risk(metrics: Dict[str, float], findings: List[Finding]) -> Tuple[int, str]:
    score = 0.0
    score += min(metrics.get("impacted_callers", 0), 30) * 1.0
    score += min(metrics.get("total_fan_in", 0), 20) * 0.5
    score += metrics.get("sensitive_changes", 0) * 6
    score += metrics.get("changes_without_tests", 0) * 4
    score += sum(_SEVERITY_WEIGHT.get(f.severity, 0) for f in findings)
    score = min(score, 100.0)
    level = ("low" if score < 25 else "medium" if score < 55
             else "high" if score < 80 else "critical")
    # A file with any critical finding is never "low" or "medium" risk.
    if any(f.severity == "critical" for f in findings) and level in ("low", "medium"):
        score = max(score, 55.0)
        level = "high"
    return int(round(score)), level


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class ChunkReviewResult:
    chunk: Chunk
    findings: List[Finding] = field(default_factory=list)
    agent_runs: List[str] = field(default_factory=list)


@dataclass
class FileReviewResult:
    file_path: str
    chunk_results: List[ChunkReviewResult] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)   # merged + deduped
    dropped: int = 0
    risk_score: int = 0
    risk_level: str = "low"
    dossier_tokens: int = 0
    num_chunks: int = 0


@dataclass
class CallerRef:
    """A function in another file that calls a changed function."""
    node_id: str
    qualname: str
    file: str
    line: int
    source: str           # the caller function's source (snippet, truncated)
    broken: bool = False  # flagged as broken by the breaking-change pass


@dataclass
class DependencyView:
    """For one changed function, the external callers that depend on it."""
    changed_node_id: str
    changed_qualname: str
    changed_file: str
    change_type: str
    callers: List[CallerRef] = field(default_factory=list)


@dataclass
class OverallResult:
    file_results: Dict[str, FileReviewResult] = field(default_factory=dict)
    all_findings: List[Finding] = field(default_factory=list)
    breaking: List[Finding] = field(default_factory=list)   # caller-compat issues
    dependencies: List[DependencyView] = field(default_factory=list)  # callers per changed fn
    risk_score: int = 0
    risk_level: str = "low"
    metrics: Dict[str, float] = field(default_factory=dict)
    dossier_tokens: int = 0
    dropped_total: int = 0
    profile_key: str = "deep"
    skipped_files: List[str] = field(default_factory=list)  # ignored (lock/binary/config)

    @property
    def issues(self) -> List[Finding]:
        """Concrete problems (excludes suggestions)."""
        return [f for f in self.all_findings if f.kind == "issue"]

    @property
    def suggestions(self) -> List[Finding]:
        """Optional improvements."""
        return [f for f in self.all_findings if f.kind == "suggestion"]


# ── Verifier ──────────────────────────────────────────────────────────────────

def _verify(combined_dossier: str, candidates: List[Finding],
            nova: NovaClient) -> List[Finding]:
    if not candidates:
        return []
    listing = json.dumps(
        [{"index": i, "category": f.category, "file": f.file, "line": f.line,
          "title": f.title, "evidence": f.evidence}
         for i, f in enumerate(candidates)],
        indent=2,
    )
    raw = nova.complete_json(
        VERIFY_SYSTEM,
        VERIFY_INSTRUCTIONS.format(dossier=combined_dossier, findings=listing),
    )
    if not isinstance(raw, list):
        return candidates
    verdict = {
        int(v["index"]): bool(v.get("valid", True))
        for v in raw
        if isinstance(v, dict) and "index" in v
    }
    return [f for i, f in enumerate(candidates) if verdict.get(i, True)]


# ── Per-chunk review ──────────────────────────────────────────────────────────

def _review_chunk(
    chunk: Chunk,
    cg: CodeGraph,
    embed_index: Optional["EmbeddingIndex"],
    nova: NovaClient,
    active_agents: list,
) -> ChunkReviewResult:
    result = ChunkReviewResult(chunk=chunk)
    for agent in active_agents:
        try:
            findings = agent.run(chunk.dossier, cg, embed_index, nova)
            result.findings.extend(findings)
            result.agent_runs.append(agent.name)
        except Exception as e:
            log.exception("Agent %s failed on chunk %s", agent.name, getattr(chunk, 'chunk_index', '?'))
            result.agent_runs.append(f"{agent.name}:ERROR:{e}")
    return result


# ── Per-file review ───────────────────────────────────────────────────────────

def run_file_review(
    file_path: str,
    file_diff: FileDiff,
    file_changes: List[ChangedNode],
    cg: CodeGraph,
    blast: BlastResult,
    nova: NovaClient,
    embed_index: Optional["EmbeddingIndex"] = None,
    token_budget: int = 12000,
    verify: bool = True,
    agents: list = None,
    profile: Optional[ReviewProfile] = None,
) -> FileReviewResult:
    result = FileReviewResult(file_path=file_path)

    # Deterministic syntax gate — no LLM tokens spent on unparseable files.
    syntax_findings = _syntax_check(cg, file_diff)

    # Resolve agent sets: profile governs the split; agents= is a fallback
    if profile is not None:
        pass1_agents = build_agents(profile.whole_file_agent_keys)
        pass2_agents = build_agents(profile.changed_fn_agent_keys)
    else:
        fallback = agents if agents is not None else ALL_AGENTS
        pass1_agents = fallback
        pass2_agents = fallback

    all_candidates: List[Finding] = []

    # Syntax findings go in first — they survive verification regardless.
    all_candidates.extend(syntax_findings)

    # ── PASS 1: whole-file sweep ──────────────────────────────────────────────
    # Agents see the entire file (full source, + markers on changed lines).
    # Good for: architecture patterns, performance hotspots, broad security sweep.
    file_chunks = make_file_chunks(cg, file_path, file_diff, token_budget=token_budget)
    result.num_chunks = len(file_chunks)

    if not file_chunks:
        # Syntax-broken or unreadable: surface the syntax findings and return.
        if syntax_findings:
            result.findings = syntax_findings
            result.risk_score, result.risk_level = _risk({}, syntax_findings)
        return result

    build_all_file_chunk_dossiers(file_chunks, cg, file_path)
    result.dossier_tokens = sum(_toks(c.dossier) for c in file_chunks)

    for chunk in file_chunks:
        cr = _review_chunk(chunk, cg, embed_index, nova, pass1_agents)
        result.chunk_results.append(cr)
        all_candidates.extend(cr.findings)

    # ── PASS 2: changed-function + dependency sweep ───────────────────────────
    # Agents see only the functions containing changed lines, plus their direct
    # callers, callees, and covering tests from the code graph.
    # Good for: correctness (caller contracts), security (data-flow context),
    #           API contract breaks (callers shown explicitly).
    if file_diff.added_lines:
        prox_chunks = make_chunks(cg, file_path, file_diff, file_changes)
        build_all_chunk_dossiers(
            prox_chunks, cg, blast, file_changes,
            embed_index=embed_index,
            cross_file=True,
        )
        result.num_chunks += len(prox_chunks)
        for chunk in prox_chunks:
            cr = _review_chunk(chunk, cg, embed_index, nova, pass2_agents)
            result.chunk_results.append(cr)
            all_candidates.extend(cr.findings)

    if not all_candidates:
        result.risk_score, result.risk_level = 0, "low"
        return result

    # ── verify + dedup ────────────────────────────────────────────────────────
    # Include Pass 2 dossiers so the verifier has caller/callee context for
    # findings that cite it; cap total to avoid blowing the verifier window.
    _MAX_VERIFY_TOKS = 24_000
    _CHARS = _MAX_VERIFY_TOKS * 4
    combined_parts = [c.dossier for c in file_chunks]
    if file_diff.added_lines:
        combined_parts += [c.dossier for c in prox_chunks]
    combined = "\n\n---\n\n".join(combined_parts)
    if len(combined) > _CHARS:
        combined = combined[-_CHARS:]  # keep most recent (Pass 2) context
    survivors = all_candidates
    if verify:
        survivors = _verify(combined, all_candidates, nova)
        result.dropped = len(all_candidates) - len(survivors)

    seen: set = set()
    deduped: List[Finding] = []
    for f in survivors:
        key = (f.file, f.line, f.title[:40])
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    deduped.sort(key=lambda f: order.get(f.severity, 9))
    result.findings = deduped

    # ── per-file risk ─────────────────────────────────────────────────────────
    file_node_ids = {ch.node_id for ch in file_changes}
    file_metrics: Dict[str, float] = {
        "changed_nodes": float(len(file_changes)),
        "total_fan_in": float(
            sum(cg.fan_in(n) for n in file_node_ids if cg.has(n))
        ),
        "impacted_callers": float(len({
            c
            for nid in file_node_ids
            for c in (
                blast.per_change[nid].callers
                if nid in blast.per_change else {}
            )
        })),
        "sensitive_changes": float(sum(
            1 for nid in file_node_ids
            if cg.has(nid) and any(
                k in cg.node(nid).get("name", "").lower()
                for k in ("auth", "password", "token", "secret", "payment",
                          "credential", "api_key", "encrypt", "decrypt")
            )
        )),
        "changes_without_tests": float(sum(
            1 for ch in file_changes
            if cg.has(ch.node_id)
            and not blast.per_change.get(ch.node_id, _EmptyImpact()).tests
        )),
    }
    result.risk_score, result.risk_level = _risk(file_metrics, deduped)
    return result


class _EmptyImpact:
    tests: set = frozenset()


def _toks(text: str) -> int:
    return max(1, len(text) // 4)


def _syntax_check(cg: CodeGraph, fd: FileDiff) -> List[Finding]:
    """Deterministic parse gate for Python files.

    A file that won't parse is a guaranteed defect — flag it without spending any
    LLM tokens, and prevent it from being silently dropped by the num_chunks==0
    skip. Returns [] for non-Python files or files that parse cleanly.
    """
    if not fd.path.endswith(".py"):
        return []
    src = cg.source_lines(fd.path, 1, 10**9)  # whole file
    if not src.strip():
        return []
    try:
        ast.parse(src)
        return []
    except SyntaxError as e:
        line = e.lineno or (min(fd.added_lines) if fd.added_lines else 0)
        return [Finding(
            category="syntax",
            severity="critical",
            file=fd.path,
            line=int(line),
            title="File does not parse (SyntaxError)",
            explanation=(
                f"{fd.path} cannot be parsed as Python ({e.msg} at line {e.lineno}). "
                f"It will not import or run, so nothing else in it can be trusted "
                f"until the error is fixed."
            ),
            evidence=(e.text or "").strip()[:200],
            recommendation="Fix the syntax error so the file parses.",
            kind="issue",
        )]

# ── Breaking-change (caller compatibility) pass ───────────────────────────────

_MAX_DEP_CALLERS = 12     # max external callers sent to LLM per changed node
_MAX_CALLER_SRC = 1500   # chars per caller source in the prompt


def _norm_path(p: str) -> str:
    """Normalize a path string for comparison (forward slashes, strip leading ./)."""
    return p.replace("\\", "/").lstrip("./")


def _paths_match(graph_path: str, llm_path: str) -> bool:
    """True when graph_path and llm_path refer to the same file.

    The LLM sometimes emits just the filename, a relative path with or without
    leading './', or an absolute path.  We try three levels of match.
    """
    gn = _norm_path(graph_path)
    ln = _norm_path(llm_path)
    if gn == ln:
        return True
    # basename fallback (e.g. LLM says "foo.py", graph has "src/foo.py")
    return gn.split("/")[-1] == ln.split("/")[-1]


def _caller_line(cg: CodeGraph, caller_id: str) -> int:
    return int(cg.node(caller_id).get("start_line", 0)) if cg.has(caller_id) else 0


def run_dependency_check(
    cg: CodeGraph,
    changes: List[ChangedNode],
    blast: BlastResult,
    file_diffs: List[FileDiff],
    diff_text: str,
    nova: NovaClient,
    card_cache: Optional[Dict[str, IdentityCard]] = None,
) -> Tuple[List[Finding], List["DependencyView"]]:
    """Flag callers in OTHER files that a changed signature breaks and that the
    PR did not update.

    For each changed function/route whose signature changed (or that was added),
    gather its direct callers that live in a different file and were NOT modified
    in this PR, then ask the model which of those call sites are now broken.

    Returns (findings, dependency_views). The views list every examined external
    caller (name + source) so the UI can show "what uses this changed function",
    independent of whether a problem was found.
    """
    if card_cache is None:
        card_cache = {}
    out: List[Finding] = []
    views: List[DependencyView] = []

    for ch in changes:
        if ch.change_type not in ("signature", "added"):
            continue
        if not cg.has(ch.node_id):
            continue
        imp = blast.per_change.get(ch.node_id)
        if not imp:
            continue
        changed_node = cg.node(ch.node_id)
        changed_path = changed_node.get("path")

        ext_callers: List[str] = []
        for caller, dist in imp.callers.items():
            if dist != 1 or not cg.has(caller):
                continue
            if cg.node(caller).get("path") == changed_path:
                continue                      # same file → normal review covers it
            if node_was_modified(file_diffs, cg, caller):
                continue                      # caller already updated in this PR
            ext_callers.append(caller)

        if not ext_callers:
            continue

        # Cap callers sent to LLM to avoid context-window overflow.
        ext_callers = ext_callers[:_MAX_DEP_CALLERS]

        # record the dependency view (callers + their source) for the UI
        view = DependencyView(
            changed_node_id=ch.node_id,
            changed_qualname=changed_node.get("qualname") or changed_node.get("name", ""),
            changed_file=changed_path or ch.file_path,
            change_type=ch.change_type,
            callers=[
                CallerRef(
                    node_id=c,
                    qualname=cg.node(c).get("qualname") or cg.node(c).get("name", ""),
                    file=cg.node(c).get("path", ""),
                    line=_caller_line(cg, c),
                    source=cg.source(c)[:2000],
                )
                for c in ext_callers
            ],
        )
        views.append(view)

        card = build_identity_card(cg, ch.node_id, ch.change_type,
                                   nova=nova, cache=card_cache)
        if card is None:
            continue
        old_sig = old_signature_for(diff_text, cg, ch.node_id)
        callers_block = "\n\n".join(
            _slice_node_capped(cg, c,
                               f"CALLER ({cg.node(c).get('path')}:{_caller_line(cg, c)})",
                               _MAX_CALLER_SRC)
            for c in ext_callers
        )
        prompt = DEPENDENCY_INSTRUCTIONS.format(
            schema=FINDINGS_SCHEMA,
            card=render_card(card, old_sig),
            callers=callers_block,
        )
        try:
            raw = nova.complete(DEPENDENCY_SYSTEM, prompt)
        except Exception:
            log.exception("Dependency check failed for node %s", ch.node_id)
            continue
        broken_files = set()
        for f in _parse_findings(raw):
            f.category = "breaking-change"
            f.kind = "issue"
            if f.severity not in ("critical", "high"):
                f.severity = "high"
            out.append(f)
            broken_files.add((f.file, f.line))
        # mark which callers were flagged broken — normalize paths before comparing
        for cref in view.callers:
            if any(_paths_match(cref.file, bf) and abs(cref.line - bl) <= 50
                   for bf, bl in broken_files):
                cref.broken = True

    return out, views


# ── Overall review ────────────────────────────────────────────────────────────

def run_review(
    cg: CodeGraph,
    changes: List[ChangedNode],
    blast: BlastResult,
    diff_text: str,
    file_diffs: List[FileDiff],
    nova: NovaClient,
    embed_index: Optional["EmbeddingIndex"] = None,
    token_budget: int = 12000,
    profile: Optional[ReviewProfile] = None,
    agents: list = None,
    verify: Optional[bool] = None,
    progress_cb=None,
    review_config: bool = False,
) -> OverallResult:
    # Resolve the profile (default to DEEP = run everything, the legacy behavior).
    profile = profile or DEEP
    if verify is None:
        verify = profile.verify

    overall = OverallResult(metrics=blast.metrics, profile_key=profile.key)
    by_file = group_by_file(changes)

    # files we will actually review (changed, non-deleted, not generated/binary/config)
    review_files = []
    for fd in file_diffs:
        if fd.is_deleted or not fd.added_lines:
            continue
        if not should_review(fd.path, include_config=review_config):
            overall.skipped_files.append(fd.path)
            continue
        review_files.append(fd)
    n_files = len(review_files)

    # Review every changed source file IN FULL (not only files with mapped nodes),
    # so module-level edits still get a whole-file review.
    for i, fd in enumerate(review_files):
        if progress_cb:
            progress_cb("file", i, n_files, fd.path)
        file_result = run_file_review(
            file_path=fd.path,
            file_diff=fd,
            file_changes=by_file.get(fd.path, []),
            cg=cg,
            blast=blast,
            nova=nova,
            embed_index=embed_index,
            token_budget=token_budget,
            verify=verify,
            agents=agents,
            profile=profile,
        )
        if file_result.num_chunks == 0 and not file_result.findings:
            continue          # unreadable / non-source file with no content
        overall.file_results[fd.path] = file_result
        overall.all_findings.extend(file_result.findings)
        overall.dossier_tokens += file_result.dossier_tokens
        overall.dropped_total += file_result.dropped

    # Cross-file breaking-change pass (Standard / Deep).
    if profile.caller_compat:
        if progress_cb:
            progress_cb("dependency", n_files, n_files, "")
        card_cache: Dict[str, IdentityCard] = {}
        breaking, deps = run_dependency_check(
            cg, changes, blast, file_diffs, diff_text, nova, card_cache=card_cache,
        )
        overall.breaking = breaking
        overall.dependencies = deps
        overall.all_findings.extend(breaking)

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    overall.all_findings.sort(key=lambda f: order.get(f.severity, 9))
    overall.risk_score, overall.risk_level = _risk(blast.metrics, overall.all_findings)
    return overall


# ── Report formatter ──────────────────────────────────────────────────────────

def _fmt_finding(f: Finding, i: int) -> List[str]:
    out = [f"\n### {i}. [{f.severity.upper()}] {f.title}",
           f"`{f.category}` · `{f.file}:{f.line}`",
           f"\n{f.explanation}"]
    if f.evidence:
        out.append(f"\n**Evidence:** `{f.evidence}`")
    if f.recommendation:
        out.append(f"\n**Fix:** {f.recommendation}")
    return out


def format_report(result: OverallResult) -> str:
    lines = ["# PR Review Report", ""]
    lines.append(f"_Review depth: **{result.profile_key.upper()}**_")

    # severity summary (counts, not a risk score)
    sev = {s: 0 for s in ("critical", "high", "medium", "low", "info")}
    file_issues = [f for fr in result.file_results.values() for f in fr.findings
                   if f.kind == "issue"]
    for f in file_issues:
        sev[f.severity] = sev.get(f.severity, 0) + 1
    lines.append(
        f"**Critical: {sev['critical']} · High: {sev['high']} · "
        f"Medium: {sev['medium']} · Low: {sev['low']} · "
        f"Breaking: {len(result.breaking)} · Suggestions: {len(result.suggestions)}**"
    )
    lines.append(
        f"_Changed nodes: {int(result.metrics.get('changed_nodes', 0))} · "
        f"verifier dropped total: {result.dropped_total} · "
        f"~{result.dossier_tokens} tokens_"
    )
    lines.append("")

    # ── Breaking changes (cross-file) first ───────────────────────────────────
    if result.breaking:
        lines.append("---\n## ⚠️ Breaking changes (callers not updated)\n")
        for i, f in enumerate(result.breaking, 1):
            lines.extend(_fmt_finding(f, i))
        lines.append("")

    if not result.file_results:
        lines.append("No source files with mapped changes.")
        return "\n".join(lines)

    # ── Issues grouped per file ───────────────────────────────────────────────
    lines.append("---\n## Issues by file\n")
    for fpath, fr in sorted(result.file_results.items()):
        issues = [f for f in fr.findings if f.kind == "issue"]
        lines.append(
            f"### `{fpath}`  "
            f"— risk: {fr.risk_level.upper()} ({fr.risk_score}/100)  "
            f"— {fr.num_chunks} chunk(s)"
        )
        if fr.dropped:
            lines.append(f"_Verifier dropped {fr.dropped} finding(s)._")
        if not issues:
            lines.append("No issues found.\n")
            continue
        for i, f in enumerate(issues, 1):
            lines.extend(_fmt_finding(f, i))
        lines.append("")

    # ── Suggestions (improvements) last ───────────────────────────────────────
    suggestions = result.suggestions
    lines.append("---\n## Suggestions\n")
    if not suggestions:
        lines.append("_No suggestions._")
    else:
        for i, f in enumerate(suggestions, 1):
            lines.append(f"\n### {i}. {f.title}")
            lines.append(f"`{f.category}` · `{f.file}:{f.line}`")
            lines.append(f"\n{f.explanation}")
            if f.recommendation:
                lines.append(f"\n**Suggested change:** {f.recommendation}")

    return "\n".join(lines)
