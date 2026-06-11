"""Per-file specialist agent orchestration (whole-file review).

Pipeline per changed file:
    1. Split the WHOLE file into definition-aware chunks  (context.make_file_chunks)
       — one chunk if the file fits the budget, else split on function boundaries
    2. Render each chunk (full source, changed lines marked '+')
    3. For each chunk, run every active agent   (agents.BaseAgent.run)
       — each agent has an agentic tool loop to query the graph mid-reasoning
    4. Merge findings from all chunks/agents
    5. Deduplicate by (file, line, title)
    6. Run evidence verifier over the combined dossier
    7. Cross-file breaking-change pass over the changed (+) functions only

The model sees the entire file so it can judge whether the file works as a whole,
while findings stay pinned to line numbers and prioritize the changed lines.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

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
    _slice_node,
    build_all_file_chunk_dossiers,
    build_dossier,
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
from .llm import NovaClient, _extract_json
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
) -> FileReviewResult:
    result = FileReviewResult(file_path=file_path)
    active_agents = agents if agents is not None else ALL_AGENTS

    # 1. split the WHOLE file into definition-aware chunks (one if it fits)
    chunks = make_file_chunks(cg, file_path, file_diff, token_budget=token_budget)
    result.num_chunks = len(chunks)

    if not chunks:
        return result

    # 2. render each chunk's dossier (full source, changed lines marked '+')
    build_all_file_chunk_dossiers(chunks, cg, file_path)
    result.dossier_tokens = sum(_toks(c.dossier) for c in chunks)

    # 3. review each chunk independently
    all_candidates: List[Finding] = []
    for chunk in chunks:
        cr = _review_chunk(chunk, cg, embed_index, nova, active_agents)
        result.chunk_results.append(cr)
        all_candidates.extend(cr.findings)

    if not all_candidates:
        result.risk_score, result.risk_level = 0, "low"
        return result

    # 4. verifier over the combined dossier (all chunks joined)
    combined = "\n\n---\n\n".join(c.dossier for c in chunks)
    survivors = all_candidates
    if verify:
        survivors = _verify(combined, all_candidates, nova)
        result.dropped = len(all_candidates) - len(survivors)

    # 5. deduplicate
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

    # 6. per-file risk
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


# ── Breaking-change (caller compatibility) pass ───────────────────────────────

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
            _slice_node(cg, c, f"CALLER ({cg.node(c).get('path')}:{_caller_line(cg, c)})")
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
            continue
        broken_files = set()
        for f in _parse_findings(raw):
            f.category = "breaking-change"
            f.kind = "issue"
            if f.severity not in ("critical", "high"):
                f.severity = "high"
            out.append(f)
            broken_files.add((f.file, f.line))
        # mark which callers were flagged broken (best-effort by file:line)
        for cref in view.callers:
            if any(cref.file == bf and abs(cref.line - bl) <= 50
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
    if agents is None:
        agents = build_agents(profile.agent_keys)
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
        )
        if file_result.num_chunks == 0:
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
