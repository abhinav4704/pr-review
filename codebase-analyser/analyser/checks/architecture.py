"""Architecture-level deterministic checks from in-memory CodeGraph."""

from __future__ import annotations

from collections import Counter
from typing import Dict, List

import networkx as nx

from ..graph_contract import edge_relation, is_file_node, node_kind, node_name


def _module_of(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "root"
    if len(parts) == 1:
        return "root"
    return parts[0]


def _import_graph(code_graph) -> nx.DiGraph:
    g = nx.DiGraph()
    for node_id, node_data in code_graph.g.nodes(data=True):
        if is_file_node(node_data):
            g.add_node(node_id)

    for src, dst, edge_data in code_graph.g.edges(data=True):
        if edge_relation(edge_data) != "imports":
            continue
        if src in g and dst in g:
            g.add_edge(src, dst)
    return g


def _module_coupling(code_graph) -> List[dict]:
    pair_counts = Counter()
    for src, dst, edge_data in code_graph.g.edges(data=True):
        if edge_relation(edge_data) != "imports":
            continue
        src_module = _module_of(code_graph.node(src).get("path", ""))
        dst_module = _module_of(code_graph.node(dst).get("path", ""))
        if src_module == dst_module:
            continue
        pair_counts[(src_module, dst_module)] += 1

    rows = [
        {"from": src, "to": dst, "edges": count}
        for (src, dst), count in pair_counts.items()
    ]
    rows.sort(key=lambda item: item["edges"], reverse=True)
    return rows[:25]


def analyze_architecture(code_graph) -> Dict[str, object]:
    """Return architecture smell summary for cycles, hotspots and entrypoints."""
    ig = _import_graph(code_graph)

    cycles = []
    try:
        for cycle in nx.simple_cycles(ig):
            if len(cycle) < 2:
                continue
            cycles.append(cycle)
            if len(cycles) >= 20:
                break
    except nx.NetworkXNoCycle:
        cycles = []

    kind_counts = Counter()
    for _nid, node_data in code_graph.g.nodes(data=True):
        kind = node_kind(node_data)
        if kind:
            kind_counts[kind] += 1

    def_nodes = [
        (node_id, data)
        for node_id, data in code_graph.g.nodes(data=True)
        if node_kind(data) in {"function", "method", "class", "route", "event"}
    ]
    hotspots = sorted(
        [
            {
                "node_id": node_id,
                "name": node_name(node_id, data),
                "path": data.get("path", ""),
                "kind": node_kind(data),
                "fan_in": code_graph.fan_in(node_id),
            }
            for node_id, data in def_nodes
        ],
        key=lambda item: item["fan_in"],
        reverse=True,
    )[:20]

    entrypoints = [
        {
            "node_id": node_id,
            "name": node_name(node_id, code_graph.node(node_id)),
            "path": code_graph.node(node_id).get("path", ""),
            "kind": node_kind(code_graph.node(node_id)),
        }
        for node_id in (list(code_graph.routes()) + list(code_graph.events()))
    ]

    return {
        "kind_counts": dict(kind_counts),
        "import_cycles": cycles,
        "module_coupling": _module_coupling(code_graph),
        "hotspots": hotspots,
        "entrypoints": entrypoints,
    }
