from __future__ import annotations

import networkx as nx

from analyser.checks.architecture import analyze_architecture
from analyser.graph_contract import edge_relation, is_import_edge


class _FakeCodeGraph:
    def __init__(self) -> None:
        self.g = nx.DiGraph()

    def node(self, node_id: str):
        return self.g.nodes[node_id]

    def fan_in(self, node_id: str) -> int:
        return self.g.in_degree(node_id)

    def routes(self):
        return []

    def events(self):
        return []


def test_edge_relation_accepts_legacy_and_canonical_fields() -> None:
    assert edge_relation({"type": "imports"}) == "imports"
    assert edge_relation({"relation": "imports"}) == "imports"
    assert is_import_edge({"relation": "imports"})
    assert is_import_edge({"type": "imports"})


def test_architecture_accepts_relation_based_import_edges() -> None:
    graph = _FakeCodeGraph()

    graph.g.add_node("a.py", kind="file", path="a.py")
    graph.g.add_node("b.py", kind="file", path="b.py")
    graph.g.add_edge("a.py", "b.py", relation="imports")

    result = analyze_architecture(graph)
    couplings = result["module_coupling"]

    assert len(couplings) == 0
    assert result["kind_counts"].get("file") == 2
