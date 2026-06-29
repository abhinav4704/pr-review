"""Deterministic breakage checks using reverse dependency expansion."""

from __future__ import annotations

from typing import Dict, List, Set, Tuple

from .common import should_exclude_path
from ..graph_contract import node_kind, node_name

SENSITIVE_TOKENS = {
    "auth",
    "token",
    "secret",
    "password",
    "billing",
    "payment",
    "session",
    "permission",
    "admin",
}
def _candidate_nodes(code_graph) -> List[str]:
    entrypoints = set(code_graph.routes()) | set(code_graph.events())
    defs = [
        node_id
        for node_id, node_data in code_graph.g.nodes(data=True)
        if node_kind(node_data) in {"function", "method", "route", "event", "class"}
        and not should_exclude_path(node_data.get("path", ""))
    ]

    scored: List[Tuple[int, str]] = []
    for node_id in defs:
        node_data = code_graph.node(node_id)
        fan_in = code_graph.fan_in(node_id)
        name = node_name(node_id, node_data).lower()
        sensitive = any(token in name for token in SENSITIVE_TOKENS)
        entrypoint_bonus = 5 if node_id in entrypoints else 0
        sensitive_bonus = 3 if sensitive else 0
        scored.append((fan_in + entrypoint_bonus + sensitive_bonus, node_id))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [node_id for _, node_id in scored]


def analyze_breakage(code_graph, depth: int = 2, top_n: int = 20) -> Dict[str, object]:
    """Return blast-radius findings and hotspot summary for risky nodes."""
    candidates = _candidate_nodes(code_graph)[:top_n]
    blast_radius: List[dict] = []

    for node_id in candidates:
        deps = code_graph.reverse_dependents(
            node_id,
            depth=depth,
            exclude_ambiguous=True,
        )
        if not deps:
            continue

        node_data = code_graph.node(node_id)
        source_name = node_name(node_id, node_data)
        severity = "high" if len(deps) >= 15 else "medium" if len(deps) >= 6 else "low"

        impacted = []
        for dep_id, hop in sorted(deps.items(), key=lambda item: item[1]):
            dep_data = code_graph.node(dep_id)
            if should_exclude_path(dep_data.get("path", "")):
                continue
            impacted.append(
                {
                    "node_id": dep_id,
                    "name": node_name(dep_id, dep_data),
                    "path": dep_data.get("path", ""),
                    "kind": node_kind(dep_data),
                    "hops": hop,
                }
            )

        if not impacted:
            continue

        blast_radius.append(
            {
                "source_node_id": node_id,
                "source_name": source_name,
                "source_path": node_data.get("path", ""),
                "source_kind": node_kind(node_data),
                "fan_in": code_graph.fan_in(node_id),
                "impacted_count": len(impacted),
                "severity": severity,
                "summary": (
                    f"Changing {source_name} can impact {len(impacted)} reverse dependents "
                    f"within {depth} hop(s)."
                ),
                "impacted": impacted,
            }
        )

    blast_radius.sort(key=lambda item: item["impacted_count"], reverse=True)

    hotspots = [
        {
            "node_id": node_id,
            "name": node_name(node_id, code_graph.node(node_id)),
            "path": code_graph.node(node_id).get("path", ""),
            "kind": node_kind(code_graph.node(node_id)),
            "fan_in": code_graph.fan_in(node_id),
        }
        for node_id in _candidate_nodes(code_graph)[:10]
    ]

    return {
        "blast_radius": blast_radius,
        "hotspots": hotspots,
    }
