"""Analyser-owned graph contract helpers.

This module normalizes graph semantics consumed by the analyser so checks do
not depend on raw backend field choices.
"""

from __future__ import annotations

from typing import Any, Mapping


DEFINITION_KINDS = {"function", "method", "class", "route", "event", "table"}


def node_kind(node_data: Mapping[str, Any]) -> str:
    return str(node_data.get("kind") or "").strip().lower()


def node_name(node_id: str, node_data: Mapping[str, Any]) -> str:
    return str(node_data.get("qualname") or node_data.get("name") or node_id)


def edge_relation(edge_data: Mapping[str, Any], default: str = "") -> str:
    relation = str(edge_data.get("relation") or edge_data.get("type") or default)
    return relation.strip().lower()


def is_import_edge(edge_data: Mapping[str, Any]) -> bool:
    return edge_relation(edge_data) == "imports"


def is_file_node(node_data: Mapping[str, Any]) -> bool:
    return node_kind(node_data) == "file"


def is_definition_node(node_data: Mapping[str, Any]) -> bool:
    return node_kind(node_data) in DEFINITION_KINDS
