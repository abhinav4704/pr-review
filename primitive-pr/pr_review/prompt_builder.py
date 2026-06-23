"""Prompt and dependency artifact builders for changed functions."""

from __future__ import annotations

import os
from collections import deque
from typing import Dict, List, Set

from .diff import parse_diff
from .graph import CodeGraph
from .graph import DEFAULT_IMPACT_RELATIONS
from .graph_contract import edge_relation, is_ambiguous_confidence
from .pr_passes import number_lines, read_source

_DEPENDENT_KINDS = {
    "function", "method", "route", "event", "class", "table",
    "field", "interface", "type_alias", "enum", "variable", "service",
}
_CHANGED_KINDS = {
    "function", "method", "route", "event", "class", "table",
    "field", "interface", "type_alias", "enum", "variable", "service",
}


_MIN_REASONABLE_BLOCK_LINES = 4
_FALLBACK_BLOCK_WINDOW = 120


def _path_candidates(cg: CodeGraph, path: str) -> List[str]:
    """Return candidate graph paths for a possibly mismatched diff path."""
    if not path:
        return []

    graph_paths: Set[str] = {
        str(d.get("path"))
        for _, d in cg.g.nodes(data=True)
        if d.get("path")
    }

    norm = path.replace("\\", "/")
    stripped = norm.lstrip("./")

    out: List[str] = []
    for candidate in (norm, stripped):
        if candidate and candidate in graph_paths and candidate not in out:
            out.append(candidate)

    try:
        rel = os.path.relpath(norm, cg.root).replace("\\", "/")
    except Exception:
        rel = ""
    if rel and rel in graph_paths and rel not in out:
        out.append(rel)

    suffix_matches = [
        gp for gp in graph_paths
        if gp == stripped or gp.endswith("/" + stripped) or stripped.endswith("/" + gp)
    ]
    if len(suffix_matches) == 1 and suffix_matches[0] not in out:
        out.append(suffix_matches[0])

    base = os.path.basename(stripped)
    if base:
        basename_matches = [gp for gp in graph_paths if os.path.basename(gp) == base]
        if len(basename_matches) == 1 and basename_matches[0] not in out:
            out.append(basename_matches[0])

    return out or [path]


def _fallback_node_for_line(cg: CodeGraph, path: str, line: int) -> str | None:
    """Fallback mapper when exact line-span lookup fails.

    Some extractors only provide declaration-line ranges, so body-line changes
    may not hit `node_for_line`. In that case, map to the nearest definition
    that starts at or before the changed line in the same file.
    """
    candidates: List[tuple[int, int, str]] = []
    for nid in cg.defs_in_file(path):
        if not cg.has(nid):
            continue
        d = cg.node(nid)
        start = int(d.get("start_line") or 0)
        end = int(d.get("end_line") or 0)
        if start <= 0:
            continue
        span = (end - start) if end >= start else 0
        # Prefer definitions at/before the changed line.
        gap = line - start if start <= line else 10_000 + (start - line)
        candidates.append((gap, span, nid))

    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1], t[2]))
    return candidates[0][2]


def _changed_nodes_for_file(cg: CodeGraph, path: str, added_lines: Set[int]) -> List[str]:
    nodes: List[str] = []
    seen: Set[str] = set()
    candidate_paths = _path_candidates(cg, path)
    for line in sorted(added_lines):
        nid = None
        for cand_path in candidate_paths:
            nid = cg.node_for_line(cand_path, line)
            if nid:
                break
        if not nid:
            for cand_path in candidate_paths:
                nid = _fallback_node_for_line(cg, cand_path, line)
                if nid:
                    break
        if not nid or nid in seen:
            continue
        seen.add(nid)
        nodes.append(nid)
    return nodes


def _dependent_nodes(cg: CodeGraph, nid: str, depth: int) -> List[str]:
    deps = cg.reverse_dependents(nid, depth=depth)
    ranked = sorted(deps.items(), key=lambda kv: (kv[1], kv[0]))
    out: List[str] = []
    for dep_id, _distance in ranked:
        if not cg.has(dep_id):
            continue
        kind = str(cg.node(dep_id).get("kind", ""))
        if kind not in _DEPENDENT_KINDS:
            continue
        out.append(dep_id)
    return out


def _reverse_shortest_path(cg: CodeGraph, start: str, target: str, depth: int) -> List[str]:
    """Find shortest reverse-dependent node path start -> target within depth."""
    if start == target:
        return [start]

    queue: deque[tuple[str, int]] = deque([(start, 0)])
    prev: Dict[str, str] = {}
    seen: Set[str] = {start}

    while queue:
        cur, d = queue.popleft()
        if d >= depth:
            continue
        for src, _tgt, edge_data_obj in cg.g.in_edges(cur, data=True):
            relation = edge_relation(edge_data_obj)
            if relation not in DEFAULT_IMPACT_RELATIONS:
                continue
            if is_ambiguous_confidence(edge_data_obj.get("confidence")):
                continue
            if src in seen:
                continue
            seen.add(src)
            prev[src] = cur
            if src == target:
                path = [target]
                while path[-1] != start:
                    path.append(prev[path[-1]])
                path.reverse()
                return path
            queue.append((src, d + 1))

    return []


def _node_path_to_file_chain(cg: CodeGraph, node_path: List[str]) -> List[str]:
    """Convert node path to de-duplicated adjacent file path chain."""
    out: List[str] = []
    for nid in node_path:
        if not cg.has(nid):
            continue
        p = str(cg.node(nid).get("path") or "")
        if not p:
            continue
        if not out or out[-1] != p:
            out.append(p)
    return out


def _render_node_block(cg: CodeGraph, src_path: str, nid: str, heading_prefix: str) -> str:
    d = cg.node(nid)
    path = str(d.get("path") or "")
    start = int(d.get("start_line") or 0)
    end = int(d.get("end_line") or 0)
    if not path or start <= 0 or end < start:
        return ""

    # Some extractors provide narrow spans (signature-only or tiny bodies).
    # Expand to next definition boundary in the same file; otherwise use a
    # bounded fallback window for usable review context.
    span = end - start + 1
    if span < _MIN_REASONABLE_BLOCK_LINES:
        next_start = None
        for other_id in cg.defs_in_file(path):
            if other_id == nid or not cg.has(other_id):
                continue
            other_start = int(cg.node(other_id).get("start_line") or 0)
            if other_start > start and (next_start is None or other_start < next_start):
                next_start = other_start
        if next_start is not None:
            end = max(end, next_start - 1)
        else:
            end = max(end, start + _FALLBACK_BLOCK_WINDOW)

    src = read_source(src_path, path, start, end)
    if not src.strip():
        return ""
    label = d.get("qualname") or d.get("name") or nid
    numbered = number_lines(src, start)
    return (
        f"### {heading_prefix} {d.get('kind','?')}: {label} ({path}, lines {start}-{end})\n"
        f"```\n{numbered}\n```\n"
    )


def build_prompts_by_function(cg: CodeGraph, src_path: str, diff_text: str,
                              depth: int = 2,
                              max_dependents_per_changed: int = 25) -> Dict[str, str]:
    """Build one prompt per changed function/method node.

    If a file has multiple changed functions, this returns one prompt for each
    changed function in that file.
    """
    prompts: Dict[str, str] = {}
    for fd in parse_diff(diff_text):
        if fd.is_deleted or not fd.added_lines:
            continue

        changed_nodes = _changed_nodes_for_file(cg, fd.path, fd.added_lines)
        if not changed_nodes:
            continue

        for nid in changed_nodes:
            d = cg.node(nid)
            file_path = str(d.get("path") or fd.path)
            label = str(d.get("qualname") or d.get("name") or nid)
            prompt_id = f"{file_path}::{label}"

            parts: List[str] = [
                f"# PR/Commit context for {file_path}",
                "",
                "## Task",
                "Review the changed function and all dependent caller functions.",
                "Report concrete breakages and risky behavior changes.",
                "",
                "## Changed function",
            ]

            block = _render_node_block(cg, src_path, nid, "Changed")
            if block:
                parts.append(block)

            dependent_set: Set[str] = set()
            deps = _dependent_nodes(cg, nid, depth=depth)
            for dep in deps[:max_dependents_per_changed]:
                dependent_set.add(dep)

            if dependent_set:
                parts.append("## Dependent caller functions")
                for dep_nid in sorted(dependent_set):
                    dep_block = _render_node_block(cg, src_path, dep_nid, "Dependent")
                    if dep_block:
                        parts.append(dep_block)
            else:
                parts.append("## Dependent caller functions")
                parts.append(
                    "No dependent caller functions were found at the selected traversal depth."
                )

            prompts[prompt_id] = "\n".join(parts).strip()

    return prompts


def build_changed_file_chains(cg: CodeGraph, diff_text: str,
                              depth: int = 2) -> Dict[str, Dict[str, List[str] | str]]:
    """Build separate per-changed-function file dependency chains.

    Returns mapping:
      file_path::function_name -> {
        "changed_file": "...",
        "changed_function": "...",
        "dependent_files": [...],
        "chain_paths": ["a.py -> b.py", ...]
      }
    """
    chains: Dict[str, Dict[str, List[str] | str]] = {}
    for fd in parse_diff(diff_text):
        if fd.is_deleted or not fd.added_lines:
            continue

        changed_nodes = _changed_nodes_for_file(cg, fd.path, fd.added_lines)
        if not changed_nodes:
            continue

        for nid in changed_nodes:
            if not cg.has(nid):
                continue
            d = cg.node(nid)
            file_path = str(d.get("path") or fd.path)
            fn_label = str(d.get("qualname") or d.get("name") or nid)
            chain_key = f"{file_path}::{fn_label}"

            dependent_files: Set[str] = set()
            chain_paths: Set[str] = set()

            deps = cg.reverse_dependents(nid, depth=depth)
            for dep_id in deps:
                if not cg.has(dep_id):
                    continue
                dep_path = str(cg.node(dep_id).get("path") or "")
                if dep_path and dep_path != file_path:
                    dependent_files.add(dep_path)

                node_path = _reverse_shortest_path(cg, nid, dep_id, depth=depth)
                if not node_path:
                    continue
                file_chain = _node_path_to_file_chain(cg, node_path)
                if len(file_chain) >= 2:
                    chain_paths.add(" -> ".join(file_chain))

            chains[chain_key] = {
                "changed_file": file_path,
                "changed_function": fn_label,
                "dependent_files": sorted(dependent_files),
                "chain_paths": sorted(chain_paths),
            }

    return chains


def _selected_function_nodes(cg: CodeGraph, selected_files: List[str]) -> List[str]:
    """Collect changed-symbol node ids from selected files in stable order."""
    out: List[str] = []
    seen: Set[str] = set()

    for path in selected_files:
        resolved_paths = _path_candidates(cg, path)
        used_path = ""
        for cand in resolved_paths:
            if cg.defs_in_file(cand):
                used_path = cand
                break
        if not used_path:
            continue

        nids = [nid for nid in cg.defs_in_file(used_path) if cg.has(nid)]
        nids.sort(key=lambda nid: (
            int(cg.node(nid).get("start_line") or 0),
            str(cg.node(nid).get("qualname") or cg.node(nid).get("name") or nid),
        ))
        for nid in nids:
            kind = str(cg.node(nid).get("kind") or "")
            if kind not in _CHANGED_KINDS:
                continue
            if nid in seen:
                continue
            seen.add(nid)
            out.append(nid)
    return out


def build_prompts_for_selected_files(cg: CodeGraph, src_path: str,
                                     selected_files: List[str],
                                     depth: int = 2,
                                     max_dependents_per_changed: int = 25) -> Dict[str, str]:
    """Build one prompt per selected symbol in user-selected files (no diff required)."""
    prompts: Dict[str, str] = {}

    for nid in _selected_function_nodes(cg, selected_files):
        d = cg.node(nid)
        file_path = str(d.get("path") or "")
        if not file_path:
            continue

        label = str(d.get("qualname") or d.get("name") or nid)
        prompt_id = f"{file_path}::{label}"
        if prompt_id in prompts:
            start = int(d.get("start_line") or 0)
            prompt_id = f"{file_path}::{label}@{start}"

        parts: List[str] = [
            f"# Manual review context for {file_path}",
            "",
            "## Task",
            "Review this changed symbol and its dependent symbols.",
            "Report concrete breakages and risky behavior changes.",
            "",
            "## Selected symbol",
        ]

        block = _render_node_block(cg, src_path, nid, "Changed")
        if block:
            parts.append(block)

        dependent_set: Set[str] = set()
        deps = _dependent_nodes(cg, nid, depth=depth)
        for dep in deps[:max_dependents_per_changed]:
            dependent_set.add(dep)

        if dependent_set:
            parts.append("## Dependent symbols")
            for dep_nid in sorted(dependent_set):
                dep_block = _render_node_block(cg, src_path, dep_nid, "Dependent")
                if dep_block:
                    parts.append(dep_block)
        else:
            parts.append("## Dependent symbols")
            parts.append(
                "No dependent symbols were found at the selected traversal depth."
            )

        prompts[prompt_id] = "\n".join(parts).strip()

    return prompts


def build_selected_file_chains(cg: CodeGraph, selected_files: List[str],
                               depth: int = 2) -> Dict[str, Dict[str, List[str] | str]]:
    """Build per-function dependency chain summaries for selected files."""
    chains: Dict[str, Dict[str, List[str] | str]] = {}

    for nid in _selected_function_nodes(cg, selected_files):
        if not cg.has(nid):
            continue

        d = cg.node(nid)
        file_path = str(d.get("path") or "")
        if not file_path:
            continue

        fn_label = str(d.get("qualname") or d.get("name") or nid)
        chain_key = f"{file_path}::{fn_label}"
        if chain_key in chains:
            start = int(d.get("start_line") or 0)
            chain_key = f"{file_path}::{fn_label}@{start}"

        dependent_files: Set[str] = set()
        chain_paths: Set[str] = set()

        deps = cg.reverse_dependents(nid, depth=depth)
        for dep_id in deps:
            if not cg.has(dep_id):
                continue
            dep_path = str(cg.node(dep_id).get("path") or "")
            if dep_path and dep_path != file_path:
                dependent_files.add(dep_path)

            node_path = _reverse_shortest_path(cg, nid, dep_id, depth=depth)
            if not node_path:
                continue
            file_chain = _node_path_to_file_chain(cg, node_path)
            if len(file_chain) >= 2:
                chain_paths.add(" -> ".join(file_chain))

        chains[chain_key] = {
            "changed_file": file_path,
            "changed_function": fn_label,
            "dependent_files": sorted(dependent_files),
            "chain_paths": sorted(chain_paths),
        }

    return chains


# Backwards-compatible alias kept for callers not yet migrated.
def build_prompts_by_file(cg: CodeGraph, src_path: str, diff_text: str,
                          depth: int = 2,
                          max_dependents_per_changed: int = 25) -> Dict[str, str]:
    return build_prompts_by_function(
        cg,
        src_path,
        diff_text,
        depth=depth,
        max_dependents_per_changed=max_dependents_per_changed,
    )
