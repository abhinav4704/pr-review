"""Shared tree-sitter traversal helpers."""
from __future__ import annotations

from typing import Iterator

from tree_sitter import Node


def text(src: bytes, node: Node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def iter_descendants(node: Node) -> Iterator[Node]:
    """Pre-order walk of all descendants (excluding the node itself)."""
    stack = list(reversed(node.children))
    while stack:
        cur = stack.pop()
        yield cur
        if cur.children:
            stack.extend(reversed(cur.children))


def simple_type_name(raw: str) -> str:
    """Strip generics, array brackets and package qualifiers -> simple name."""
    raw = raw.split("<", 1)[0]
    raw = raw.replace("[]", "").strip()
    if "." in raw:
        raw = raw.rsplit(".", 1)[1]
    return raw
