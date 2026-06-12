"""Risk-adaptive blast radius using all 7 edge types."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Set

from .diff import ChangedNode
from .graph import CodeGraph

_SENSITIVE = re.compile(
    r"(auth|login|token|password|passwd|secret|credential|payment|pay|charge|"
    r"refund|admin|grant|permission|delete|drop|truncate|exec|eval|query|sql|"
    r"migrate|encrypt|decrypt|sign|verify|session|cookie|api_key|access|sudo|"
    r"privilege|role|jwt|oauth|key|cert|tls|ssl)",
    re.IGNORECASE,
)


def _is_sensitive(cg: CodeGraph, node_id: str) -> bool:
    d = cg.node(node_id)
    return bool(_SENSITIVE.search(d.get("name", ""))
                or _SENSITIVE.search(d.get("path", ""))
                or d.get("kind") in ("route", "table", "event"))


def _reverse_depth(cg: CodeGraph, change: ChangedNode) -> int:
    fan = cg.fan_in(change.node_id)
    depth = 2
    if fan >= 5 or _is_sensitive(cg, change.node_id):
        depth = 3
    if change.change_type in ("signature", "added"):
        depth = min(depth + 1, 4)
    if fan == 0 and change.change_type == "behavior":
        depth = 1
    return depth


def _bfs_reverse(cg: CodeGraph, start: str, depth: int) -> Dict[str, int]:
    """BFS over CALLS + INSTANTIATES + DECORATES (reverse = who depends on me)."""
    seen: Dict[str, int] = {}
    frontier = [start]
    dist = 0
    while frontier and dist < depth:
        dist += 1
        nxt: List[str] = []
        for n in frontier:
            for u, _, t in cg.g.in_edges(n, data="type"):
                if t in ("calls", "instantiates", "decorates") and u not in seen and u != start:
                    seen[u] = dist
                    nxt.append(u)
        frontier = nxt
    return seen


def _bfs_forward(cg: CodeGraph, start: str, depth: int = 1) -> Dict[str, int]:
    """BFS over CALLS + INHERITS forward (what does this depend on)."""
    seen: Dict[str, int] = {}
    frontier = [start]
    dist = 0
    while frontier and dist < depth:
        dist += 1
        nxt: List[str] = []
        for n in frontier:
            for _, v, t in cg.g.out_edges(n, data="type"):
                if t in ("calls", "inherits", "instantiates") and v not in seen and v != start:
                    seen[v] = dist
                    nxt.append(v)
        frontier = nxt
    return seen


def _covering_tests(cg: CodeGraph, node_id: str) -> Set[str]:
    tests: Set[str] = set()
    for caller, dist in _bfs_reverse(cg, node_id, depth=3).items():
        if cg.node(caller).get("is_test"):
            tests.add(caller)
    simple = cg.node(node_id).get("name", "")
    # Require the function name to appear as a word segment in the test name,
    # and skip names shorter than 4 chars to avoid noise (e.g. "get" in "test_widget").
    if len(simple) >= 4:
        pattern = re.compile(r"(?:^|_)" + re.escape(simple.lower()) + r"(?:_|$)")
        for n, d in cg.g.nodes(data=True):
            if d.get("is_test") and pattern.search(d.get("name", "").lower()):
                tests.add(n)
    return tests


@dataclass
class Impact:
    callers: Dict[str, int] = field(default_factory=dict)
    callees: Dict[str, int] = field(default_factory=dict)
    tests: Set[str] = field(default_factory=set)
    inheritors: List[str] = field(default_factory=list)


@dataclass
class BlastResult:
    per_change: Dict[str, Impact] = field(default_factory=dict)
    metrics: Dict[str, float] = field(default_factory=dict)

    def all_context_nodes(self) -> Set[str]:
        nodes: Set[str] = set()
        for imp in self.per_change.values():
            nodes |= set(imp.callers) | set(imp.callees) | imp.tests
            nodes |= set(imp.inheritors)
        return nodes


def blast_radius(cg: CodeGraph, changes: List[ChangedNode]) -> BlastResult:
    result = BlastResult()
    total_fan_in = 0
    sensitive_hits = 0
    impacted: Set[str] = set()

    for ch in changes:
        if not cg.has(ch.node_id):
            continue
        depth = _reverse_depth(cg, ch)
        imp = Impact()
        imp.callers = _bfs_reverse(cg, ch.node_id, depth=depth)
        imp.callees = _bfs_forward(cg, ch.node_id, depth=1)
        imp.tests = _covering_tests(cg, ch.node_id)
        imp.inheritors = cg.inheritors(ch.node_id)
        result.per_change[ch.node_id] = imp

        total_fan_in += cg.fan_in(ch.node_id)
        sensitive_hits += int(_is_sensitive(cg, ch.node_id))
        impacted |= set(imp.callers)

    changed_with_tests = sum(
        1 for ch in changes
        if cg.has(ch.node_id) and result.per_change.get(ch.node_id, Impact()).tests
    )
    result.metrics = {
        "changed_nodes": float(len(changes)),
        "total_fan_in": float(total_fan_in),
        "impacted_callers": float(len(impacted)),
        "sensitive_changes": float(sensitive_hits),
        "changes_without_tests": float(len(changes) - changed_with_tests),
    }
    return result
