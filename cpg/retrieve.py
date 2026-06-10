"""Source slicing and risk-adaptive graph retrieval.

Public API
----------
slice_node(node, source_map) -> str
    Return the source lines that define node.

subgraph(seed_ids, nodes, edges, max_hops, edge_filter=None)
    BFS from seeds; return (node_subset, edge_subset).

context_for(seed_ids, nodes, edges, source_map, policy="risk", token_limit=4000)
    Orchestrate: compute per-seed hop depth via risk policy, BFS, slice sources,
    deduplicate, token-budget trim, return list of context dicts.

Risk policy
-----------
A node is "high risk" if any edge incident to it carries a type in the HIGH_RISK_RELS
set.  High-risk nodes get max_hops=3; all others get max_hops=1.
"""
from __future__ import annotations

from collections import deque

# Edge types that elevate a node's risk level.
HIGH_RISK_RELS = {"GUARDED_BY", "WRITES_TABLE", "DEPENDS_ON", "CALLS_ENDPOINT"}


# ---------------------------------------------------------------------------
# Source slicing
# ---------------------------------------------------------------------------

def slice_node(node: dict, source_map: dict[str, str]) -> str:
    """Return the source text for *node* using *source_map*.

    *source_map* maps relpath -> full source string (as returned by open().read()).
    Falls back to an empty string when the file is not in the map or the node
    lacks line_start / line_end attributes.
    """
    relpath = node.get("file", "")
    source = source_map.get(relpath, "")
    if not source:
        return ""
    lines = source.splitlines()
    start = node.get("line_start", node.get("line", 1))
    end = node.get("line_end", start)
    # Convert to 0-based slice; clamp to valid range
    lo = max(0, start - 1)
    hi = min(len(lines), end)
    return "\n".join(lines[lo:hi])


# ---------------------------------------------------------------------------
# BFS subgraph
# ---------------------------------------------------------------------------

def subgraph(
    seed_ids: list[str],
    nodes: list[dict],
    edges: list[dict],
    max_hops: int,
    edge_filter=None,
) -> tuple[list[dict], list[dict]]:
    """Return (nodes, edges) reachable from *seed_ids* within *max_hops* hops.

    Parameters
    ----------
    seed_ids:    Starting node IDs.
    nodes:       All nodes in the graph.
    edges:       All edges in the graph.
    max_hops:    Maximum BFS depth (inclusive).
    edge_filter: Optional callable(edge) -> bool; edges for which it returns
                 False are ignored.  Useful for restricting traversal to
                 certain relationship types.
    """
    node_by_id = {n["id"]: n for n in nodes}
    adj: dict[str, list[dict]] = {}
    for e in edges:
        if edge_filter and not edge_filter(e):
            continue
        adj.setdefault(e["src"], []).append(e)
        adj.setdefault(e["dst"], []).append(e)

    seen_nodes: set[str] = set(seed_ids)
    seen_edges: set[tuple] = set()
    queue: deque[tuple[str, int]] = deque((sid, 0) for sid in seed_ids)

    while queue:
        nid, depth = queue.popleft()
        if depth >= max_hops:
            continue
        for e in adj.get(nid, []):
            ek = (e["src"], e["dst"], e["type"])
            if ek not in seen_edges:
                seen_edges.add(ek)
            for nbr in (e["src"], e["dst"]):
                if nbr not in seen_nodes:
                    seen_nodes.add(nbr)
                    queue.append((nbr, depth + 1))

    result_nodes = [node_by_id[nid] for nid in seen_nodes if nid in node_by_id]
    result_edges = [e for e in edges
                    if (e["src"], e["dst"], e["type"]) in seen_edges]
    return result_nodes, result_edges


# ---------------------------------------------------------------------------
# Risk policy
# ---------------------------------------------------------------------------

def _is_high_risk(node_id: str, edges: list[dict]) -> bool:
    """True when any edge incident to *node_id* is in HIGH_RISK_RELS."""
    for e in edges:
        if e["type"] in HIGH_RISK_RELS and (e["src"] == node_id or e["dst"] == node_id):
            return True
    return False


def compute_hops(node_id: str, edges: list[dict]) -> int:
    """Return the BFS depth budget for *node_id* under the risk policy."""
    return 3 if _is_high_risk(node_id, edges) else 1


# ---------------------------------------------------------------------------
# context_for — high-level entry point
# ---------------------------------------------------------------------------

def context_for(
    seed_ids: list[str],
    nodes: list[dict],
    edges: list[dict],
    source_map: dict[str, str],
    policy: str = "risk",
    token_limit: int = 4000,
) -> list[dict]:
    """Build a token-bounded context list for the given seed node IDs.

    Each returned dict has::

        {
            "id":      <node id>,
            "kind":    <node kind>,
            "name":    <node name>,
            "file":    <relpath>,
            "source":  <sliced source text>,
            "hops":    <BFS depth used>,
        }

    The list is ordered seed-first then by BFS discovery order.  Items are
    dropped tail-first to stay within *token_limit* (1 token ≈ 4 chars).

    *policy* is currently fixed to ``"risk"``; the parameter is reserved for
    future policies (e.g., ``"shallow"``, ``"deep"``).
    """
    if policy != "risk":
        raise ValueError(f"Unknown retrieval policy: {policy!r}. Only 'risk' is supported.")

    node_by_id = {n["id"]: n for n in nodes}
    seen: set[str] = set()
    ordered: list[dict] = []

    for sid in seed_ids:
        if sid not in node_by_id:
            continue
        hops = compute_hops(sid, edges) if policy == "risk" else 1
        sg_nodes, _ = subgraph([sid], nodes, edges, max_hops=hops)
        for n in sg_nodes:
            if n["id"] in seen:
                continue
            seen.add(n["id"])
            src = slice_node(n, source_map)
            ordered.append({
                "id":     n["id"],
                "kind":   n.get("kind", ""),
                "name":   n.get("name", ""),
                "file":   n.get("file", ""),
                "source": src,
                "hops":   hops,
            })

    # Token-budget trim: drop from the tail
    char_budget = token_limit * 4
    used = 0
    result: list[dict] = []
    for item in ordered:
        item_chars = len(item.get("source", "")) + len(item.get("name", "")) + 64
        if used + item_chars > char_budget:
            break
        result.append(item)
        used += item_chars

    return result
