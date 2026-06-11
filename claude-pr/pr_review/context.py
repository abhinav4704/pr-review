"""Chunk-based dossier builder.

Strategy
--------
Rather than sending one big dossier per file, we:

1. Group the file's added (+) lines into *proximity chunks* — runs of added
   lines that are within LINE_GAP lines of each other form one chunk.
   (e.g. lines [10,11,12, 25,26, 40,41,42,43] with gap=8 → 3 chunks)

2. For each chunk, build an isolated mini-dossier:
       a. The chunk's added lines (exact source, annotated with line numbers)
       b. The full source of every graph node the chunk lines fall inside
          (the enclosing function/method/class)
       c. Direct callers of those nodes (distance 1)
       d. Direct callees of those nodes (distance 1)
       e. Covering tests
       f. Semantic similar patterns (if embedding index available)
       g. Indirect callers (distance 2+) if budget remains

3. Each chunk-dossier is reviewed independently by the agents.
   Findings from all chunks are merged, deduplicated, and verified.

This gives the LLM laser-focused context for each logical change, and
findings stay pinned to exact line numbers within the chunk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Set

from .blast import BlastResult
from .diff import ChangedNode, FileDiff
from .graph import CodeGraph

if TYPE_CHECKING:
    from .embeddings import EmbeddingIndex

CHARS_PER_TOKEN = 4
LINE_GAP = 8          # added lines within this many lines of each other → same chunk
CHUNK_TOKEN_BUDGET = 6000   # max tokens per chunk dossier


def _toks(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


# ── Chunk dataclass ────────────────────────────────────────────────────────────

@dataclass
class Chunk:
    """A group of nearby added lines in a single file."""
    file_path: str
    added_lines: List[int]          # sorted list of + line numbers in this chunk
    node_ids: List[str]             # graph nodes whose spans cover these lines
    dossier: str = ""               # filled by build_chunk_dossier()
    chunk_index: int = 0            # position within the file's chunks (0-based)
    total_chunks: int = 1


# ── Proximity chunking ────────────────────────────────────────────────────────

def _group_lines(added_lines: Set[int], gap: int = LINE_GAP) -> List[List[int]]:
    """Group sorted added line numbers into proximity runs."""
    if not added_lines:
        return []
    sorted_lines = sorted(added_lines)
    groups: List[List[int]] = [[sorted_lines[0]]]
    for ln in sorted_lines[1:]:
        if ln - groups[-1][-1] <= gap:
            groups[-1].append(ln)
        else:
            groups.append([ln])
    return groups


def _nodes_for_lines(cg: CodeGraph, path: str, lines: List[int]) -> List[str]:
    """Return deduplicated graph nodes covering any of these line numbers."""
    seen: Set[str] = set()
    out: List[str] = []
    for ln in lines:
        nid = cg.node_for_line(path, ln)
        if nid and nid not in seen:
            seen.add(nid)
            out.append(nid)
    return out


# ── Per-chunk dossier builder ─────────────────────────────────────────────────

def _slice_node(cg: CodeGraph, node_id: str, label: str) -> str:
    d = cg.node(node_id)
    header = (
        f"# {label}: {d.get('qualname') or d.get('name', '')}  "
        f"({d['path']}:{d['start_line']}-{d['end_line']})"
    )
    src = cg.source(node_id)
    return f"{header}\n{src}" if src else header


def build_chunk_dossier(
    chunk: Chunk,
    cg: CodeGraph,
    blast: BlastResult,
    file_changes: List[ChangedNode],
    embed_index: Optional["EmbeddingIndex"] = None,
    token_budget: int = CHUNK_TOKEN_BUDGET,
) -> str:
    """Build a self-contained dossier for a single proximity chunk."""
    used = 0
    parts: List[str] = []
    emitted: Set[str] = set()

    def add(text: str) -> bool:
        nonlocal used
        cost = _toks(text)
        if used + cost > token_budget:
            return False
        parts.append(text)
        used += cost
        return True

    # ── header ────────────────────────────────────────────────────────────────
    lines_str = ", ".join(str(l) for l in chunk.added_lines)
    add(
        f"## CHUNK {chunk.chunk_index + 1}/{chunk.total_chunks}  "
        f"— file: {chunk.file_path}  lines: {lines_str}\n"
        f"_These are the added (+) lines in this chunk. "
        f"Review ONLY what is shown here._\n"
    )

    # ── a. the added lines with context (3 lines before first, 3 after last) ──
    ctx_start = max(1, chunk.added_lines[0] - 3)
    ctx_end   = chunk.added_lines[-1] + 3
    raw_ctx = cg.source_lines(chunk.file_path, ctx_start, ctx_end)
    if raw_ctx:
        # annotate: prefix each line with its line number, mark + lines
        annotated_lines = []
        for i, ln_text in enumerate(raw_ctx.splitlines()):
            lineno = ctx_start + i
            marker = "+" if lineno in set(chunk.added_lines) else " "
            annotated_lines.append(f"{marker} {lineno:4d}  {ln_text}")
        annotated = "\n".join(annotated_lines)
        add(f"### Added lines (in context)\n```\n{annotated}\n```\n")

    # ── b. full source of each enclosing node ─────────────────────────────────
    if chunk.node_ids:
        add("### Enclosing definitions\n")
    for nid in chunk.node_ids:
        if not cg.has(nid) or nid in emitted:
            continue
        # find change_type for this node
        ctype = next(
            (ch.change_type for ch in file_changes if ch.node_id == nid),
            "behavior",
        )
        block = _slice_node(cg, nid, f"CHANGED [{ctype}]")
        if add(f"```\n{block}\n```\n"):
            emitted.add(nid)

    # ── collect tiered context for nodes in this chunk ────────────────────────
    callers_d1: List[str] = []
    callers_far: List[str] = []
    callees_d1: List[str] = []
    tests: List[str] = []

    for nid in chunk.node_ids:
        imp = blast.per_change.get(nid)
        if not imp:
            continue
        for n, dist in imp.callers.items():
            (callers_d1 if dist == 1 else callers_far).append(n)
        callees_d1 += [n for n, dist in imp.callees.items() if dist == 1]
        tests += list(imp.tests)

    def emit_group(title: str, ids: List[str], label: str) -> None:
        wrote_hdr = False
        for n in dict.fromkeys(ids):
            if n in emitted or not cg.has(n):
                continue
            if not wrote_hdr:
                add(f"### {title}\n")
                wrote_hdr = True
            block = _slice_node(cg, n, label)
            if add(f"```\n{block}\n```\n"):
                emitted.add(n)
            else:
                break

    # ── c-e. callers, callees, tests ──────────────────────────────────────────
    emit_group("Direct callers (impacted if this changes)", callers_d1, "CALLER")
    emit_group("Direct callees (depended on by this chunk)", callees_d1, "CALLEE")
    emit_group("Covering tests", tests, "TEST")

    # ── f. semantic similar patterns ──────────────────────────────────────────
    if embed_index and embed_index._ready and used < int(token_budget * 0.80):
        query_text = raw_ctx or " ".join(
            cg.source(nid) for nid in chunk.node_ids if cg.has(nid)
        )
        if query_text.strip():
            similar = embed_index.find_similar_patterns(query_text, top_k=2)
            if similar:
                add("### Similar patterns in repo\n")
                for s in similar:
                    add(f"```\n{s}\n```\n")

    # ── g. indirect callers ───────────────────────────────────────────────────
    emit_group("Indirect callers", callers_far, "CALLER (indirect)")

    if not any("Covering tests" in p for p in parts):
        add(f"### Covering tests\n_No tests found for this chunk._\n")

    return "\n".join(parts)


# ── Public API ────────────────────────────────────────────────────────────────

def make_chunks(
    cg: CodeGraph,
    file_path: str,
    file_diff: FileDiff,
    file_changes: List[ChangedNode],
    gap: int = LINE_GAP,
) -> List[Chunk]:
    """Split a file's added lines into proximity chunks with graph nodes resolved."""
    groups = _group_lines(file_diff.added_lines, gap=gap)
    chunks: List[Chunk] = []
    for i, lines in enumerate(groups):
        nids = _nodes_for_lines(cg, file_path, lines)
        chunks.append(Chunk(
            file_path=file_path,
            added_lines=lines,
            node_ids=nids,
            chunk_index=i,
            total_chunks=len(groups),
        ))
    # backfill total_chunks now that we know it
    for ch in chunks:
        ch.total_chunks = len(chunks)
    return chunks


def build_all_chunk_dossiers(
    chunks: List[Chunk],
    cg: CodeGraph,
    blast: BlastResult,
    file_changes: List[ChangedNode],
    embed_index: Optional["EmbeddingIndex"] = None,
    token_budget: int = CHUNK_TOKEN_BUDGET,
) -> List[Chunk]:
    """Fill chunk.dossier for every chunk in the list. Returns the same list."""
    for chunk in chunks:
        chunk.dossier = build_chunk_dossier(
            chunk, cg, blast, file_changes,
            embed_index=embed_index,
            token_budget=token_budget,
        )
    return chunks


# ── Combined dossier (for overall risk / verifier) ────────────────────────────

def build_dossier(
    cg: CodeGraph,
    changes: List[ChangedNode],
    blast: BlastResult,
    diff_text: str,
    embed_index: Optional["EmbeddingIndex"] = None,
    token_budget: int = 20000,
) -> str:
    """Combined whole-PR dossier (used for risk scoring and the verifier pass)."""
    used = 0
    parts: List[str] = []
    emitted: Set[str] = set()

    def add(text: str) -> bool:
        nonlocal used
        cost = _toks(text)
        if used + cost > token_budget:
            return False
        parts.append(text)
        used += cost
        return True

    diff_section = diff_text
    if _toks(diff_section) > token_budget // 3:
        cut = (token_budget // 3) * CHARS_PER_TOKEN
        diff_section = diff_section[:cut] + "\n...[diff truncated]..."
    add("## PULL REQUEST DIFF\n```diff\n" + diff_section + "\n```\n")

    add("## CHANGED CODE\n")
    for ch in changes:
        if not cg.has(ch.node_id) or ch.node_id in emitted:
            continue
        block = _slice_node(cg, ch.node_id, f"CHANGED [{ch.change_type}]")
        if add(f"```\n{block}\n```\n"):
            emitted.add(ch.node_id)

    callers_d1: List[str] = []
    callers_far: List[str] = []
    callees_d1: List[str] = []
    tests: List[str] = []
    for imp in blast.per_change.values():
        for n, dist in imp.callers.items():
            (callers_d1 if dist == 1 else callers_far).append(n)
        callees_d1 += [n for n, dist in imp.callees.items() if dist == 1]
        tests += list(imp.tests)

    def emit_group(title: str, ids: List[str], label: str) -> None:
        wrote_hdr = False
        for n in dict.fromkeys(ids):
            if n in emitted or not cg.has(n):
                continue
            if not wrote_hdr:
                add(f"## {title}\n")
                wrote_hdr = True
            block = _slice_node(cg, n, label)
            if add(f"```\n{block}\n```\n"):
                emitted.add(n)
            else:
                break

    emit_group("DIRECT CALLERS", callers_d1, "CALLER")
    emit_group("DIRECT CALLEES", callees_d1, "CALLEE")
    emit_group("COVERING TESTS", tests, "TEST")
    emit_group("INDIRECT CALLERS", callers_far, "CALLER (indirect)")

    return "\n".join(parts)
