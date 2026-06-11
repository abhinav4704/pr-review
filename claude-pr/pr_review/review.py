"""Per-file, per-chunk specialist agent orchestration.

Pipeline per changed file:
    1. Split added lines into proximity chunks  (context.make_chunks)
    2. Build an isolated dossier per chunk      (context.build_chunk_dossier)
    3. For each chunk, run every active agent   (agents.BaseAgent.run)
       — each agent has an agentic tool loop to query the graph mid-reasoning
    4. Merge findings from all chunks/agents
    5. Deduplicate by (file, line, title)
    6. Run evidence verifier over the combined dossier
    7. Compute per-file and overall risk scores

This means findings are always grounded in a specific chunk's added lines,
and each chunk gets only its relevant context — not the whole file mixed together.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from .agents import ALL_AGENTS, Finding
from .blast import BlastResult
from .context import (
    Chunk,
    build_all_chunk_dossiers,
    build_dossier,
    make_chunks,
)
from .diff import ChangedNode, FileDiff, group_by_file
from .graph import CodeGraph
from .llm import NovaClient, _extract_json

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
class OverallResult:
    file_results: Dict[str, FileReviewResult] = field(default_factory=dict)
    all_findings: List[Finding] = field(default_factory=list)
    risk_score: int = 0
    risk_level: str = "low"
    metrics: Dict[str, float] = field(default_factory=dict)
    dossier_tokens: int = 0
    dropped_total: int = 0


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
    token_budget: int = 6000,
    verify: bool = True,
    agents: list = None,
) -> FileReviewResult:
    result = FileReviewResult(file_path=file_path)
    active_agents = agents if agents is not None else ALL_AGENTS

    # 1. build proximity chunks
    chunks = make_chunks(cg, file_path, file_diff, file_changes)
    result.num_chunks = len(chunks)

    if not chunks:
        return result

    # 2. build per-chunk dossiers
    build_all_chunk_dossiers(
        chunks, cg, blast, file_changes,
        embed_index=embed_index,
        token_budget=token_budget,
    )
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


# ── Overall review ────────────────────────────────────────────────────────────

def run_review(
    cg: CodeGraph,
    changes: List[ChangedNode],
    blast: BlastResult,
    diff_text: str,
    file_diffs: List[FileDiff],
    nova: NovaClient,
    embed_index: Optional["EmbeddingIndex"] = None,
    token_budget: int = 6000,
    verify: bool = True,
    agents: list = None,
) -> OverallResult:
    overall = OverallResult(metrics=blast.metrics)
    by_file = group_by_file(changes)
    file_diff_map = {fd.path: fd for fd in file_diffs}

    for fpath, file_changes in by_file.items():
        fd = file_diff_map.get(fpath)
        if not fd:
            continue
        file_result = run_file_review(
            file_path=fpath,
            file_diff=fd,
            file_changes=file_changes,
            cg=cg,
            blast=blast,
            nova=nova,
            embed_index=embed_index,
            token_budget=token_budget,
            verify=verify,
            agents=agents,
        )
        overall.file_results[fpath] = file_result
        overall.all_findings.extend(file_result.findings)
        overall.dossier_tokens += file_result.dossier_tokens
        overall.dropped_total += file_result.dropped

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    overall.all_findings.sort(key=lambda f: order.get(f.severity, 9))
    overall.risk_score, overall.risk_level = _risk(blast.metrics, overall.all_findings)
    return overall


# ── Report formatter ──────────────────────────────────────────────────────────

def format_report(result: OverallResult) -> str:
    lines = ["# PR Review Report", ""]
    lines.append(
        f"**Overall risk: {result.risk_level.upper()} ({result.risk_score}/100)**"
    )
    m = result.metrics
    lines.append(
        f"_Changed nodes: {int(m.get('changed_nodes', 0))} · "
        f"impacted callers: {int(m.get('impacted_callers', 0))} · "
        f"sensitive: {int(m.get('sensitive_changes', 0))} · "
        f"no-test changes: {int(m.get('changes_without_tests', 0))} · "
        f"verifier dropped total: {result.dropped_total} · "
        f"~{result.dossier_tokens} tokens_"
    )
    lines.append("")

    if not result.file_results:
        lines.append("No source files with mapped changes.")
        return "\n".join(lines)

    for fpath, fr in sorted(result.file_results.items()):
        lines.append(
            f"---\n## `{fpath}`  "
            f"— risk: {fr.risk_level.upper()} ({fr.risk_score}/100)  "
            f"— {fr.num_chunks} chunk(s)"
        )
        if fr.dropped:
            lines.append(f"_Verifier dropped {fr.dropped} finding(s)._")
        if not fr.findings:
            lines.append("No issues found.\n")
            continue
        for i, f in enumerate(fr.findings, 1):
            lines.append(f"\n### {i}. [{f.severity.upper()}] {f.title}")
            lines.append(f"`{f.category}` · `{f.file}:{f.line}`")
            lines.append(f"\n{f.explanation}")
            if f.evidence:
                lines.append(f"\n**Evidence:** `{f.evidence}`")
            if f.recommendation:
                lines.append(f"\n**Fix:** {f.recommendation}")
        lines.append("")

    return "\n".join(lines)
