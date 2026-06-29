"""Graph adapter boundary for the analyser.

Only this module should import the primitive graph backend directly.
"""

from __future__ import annotations

import os
from typing import Iterable, Tuple

from .graph_contract import is_definition_node, is_file_node
from .paths import ensure_primitive_on_path


def _validate_graph_interface(code_graph: object) -> None:
    required_attrs = ("g", "node", "fan_in", "reverse_dependents", "routes", "events")
    for attr in required_attrs:
        if not hasattr(code_graph, attr):
            raise RuntimeError(f"Code graph missing required attribute: {attr}")


def build_analyzer_graph(repo_root: str, backend: str = "primitive"):
    """Build graph through a single adapter boundary."""
    ensure_primitive_on_path()

    from pr_review.graph import build_graph

    abs_root = os.path.abspath(repo_root)
    code_graph = build_graph(abs_root, backend=backend)
    _validate_graph_interface(code_graph)
    return code_graph


def count_files_and_definitions(code_graph) -> Tuple[int, int]:
    files = 0
    definitions = 0
    for _, node_data in code_graph.g.nodes(data=True):
        if is_file_node(node_data):
            files += 1
        if is_definition_node(node_data):
            definitions += 1
    return files, definitions
