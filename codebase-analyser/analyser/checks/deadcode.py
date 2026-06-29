"""Deterministic dead-code checks (conservative)."""

from __future__ import annotations

from typing import Dict, List

from .common import should_exclude_path
from ..graph_contract import node_kind, node_name


LIVE_KINDS = {"route", "event"}
DEAD_CODE_KINDS = {"function", "method"}


def analyze_deadcode(code_graph, max_results: int = 200) -> Dict[str, List[dict]]:
    """Find likely dead functions/methods with no incoming calls."""
    candidates: List[dict] = []

    for node_id, node_data in code_graph.g.nodes(data=True):
        kind = node_kind(node_data)
        if kind not in DEAD_CODE_KINDS:
            continue
        if node_data.get("is_test"):
            continue
        if should_exclude_path(node_data.get("path", "")):
            continue
        if node_id in code_graph.routes() or node_id in code_graph.events():
            continue
        if code_graph.fan_in(node_id) > 0:
            continue

        candidates.append(
            {
                "node_id": node_id,
                "name": node_name(node_id, node_data),
                "path": node_data.get("path", ""),
                "kind": kind,
                "start_line": node_data.get("start_line", 0),
                "end_line": node_data.get("end_line", 0),
                "message": "No inbound calls found in graph; validate whether this is unused.",
            }
        )

    candidates.sort(key=lambda item: (item["path"], item["start_line"], item["name"]))
    return {"orphans": candidates[:max_results]}
