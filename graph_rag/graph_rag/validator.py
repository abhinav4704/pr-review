"""Deterministic graph validation helpers (M7 baseline)."""
from __future__ import annotations

from .models import Edge, Node


REQUIRED_RELATIONS = {
    "CALLS",
    "DECLARES",
    "USES_TYPE",
}


def validate_graph(nodes: list[Node], edges: list[Edge]) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    node_ids = [n.id for n in nodes]
    node_id_set = set(node_ids)

    if len(node_ids) != len(node_id_set):
        errors.append("duplicate node ids detected")

    dangling = 0
    for e in edges:
        if e.src not in node_id_set or e.dst not in node_id_set:
            dangling += 1
    if dangling:
        errors.append(f"dangling edges detected: {dangling}")

    bad_ranges = 0
    for n in nodes:
        if n.start_line and n.end_line and n.end_line < n.start_line:
            bad_ranges += 1
        if n.start_line == n.end_line and n.end_col and n.start_col and n.end_col < n.start_col:
            bad_ranges += 1
    if bad_ranges:
        errors.append(f"invalid source ranges detected: {bad_ranges}")

    rel_counts: dict[str, int] = {}
    for e in edges:
        rel_counts[e.type] = rel_counts.get(e.type, 0) + 1

    for rel in sorted(REQUIRED_RELATIONS):
        if rel_counts.get(rel, 0) == 0:
            warnings.append(f"required relation has zero edges: {rel}")

    fn_nodes = [n for n in nodes if n.label == "Function"]
    fn_missing_metrics = 0
    for n in fn_nodes:
        if n.loc <= 0 or n.cyclomatic <= 0:
            fn_missing_metrics += 1
    if fn_missing_metrics:
        warnings.append(f"function nodes missing core metrics: {fn_missing_metrics}")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "functions": len(fn_nodes),
            "dangling_edges": dangling,
            "invalid_ranges": bad_ranges,
            "relations": rel_counts,
        },
    }
