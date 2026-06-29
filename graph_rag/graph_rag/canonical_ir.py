"""Canonical IR adapters for deterministic pipeline flow.

Current implementation is intentionally conservative:
- Accept language extractor output (nodes/edges/refs)
- Normalize and deduplicate while preserving existing contracts
- Return canonicalized artifacts for downstream resolver/persistence

This keeps behavior stable while making the pipeline stage explicit.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Edge, Node, RawRef


@dataclass
class CanonicalBundle:
    nodes: list[Node]
    edges: list[Edge]
    refs: list[RawRef]


def from_extractor(nodes: list[Node], edges: list[Edge], refs: list[RawRef]) -> CanonicalBundle:
    """Map language extractor output into canonical graph entities.

    The first M6 step keeps this adapter lossless and additive.
    """
    return CanonicalBundle(nodes=list(nodes), edges=list(edges), refs=list(refs))


def merge_bundles(bundles: list[CanonicalBundle]) -> CanonicalBundle:
    nodes: list[Node] = []
    edges: list[Edge] = []
    refs: list[RawRef] = []

    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str, str, int, int, str]] = set()
    seen_refs: set[tuple[str, str, str, str, int, int]] = set()

    for b in bundles:
        for n in b.nodes:
            if n.id in seen_nodes:
                continue
            seen_nodes.add(n.id)
            nodes.append(n)

        for e in b.edges:
            key = (e.type, e.src, e.dst, e.evidence_line, e.evidence_col, e.evidence_file)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append(e)

        for r in b.refs:
            key = (r.type, r.src, r.target_name, r.ref_file, r.ref_line, r.ref_col)
            if key in seen_refs:
                continue
            seen_refs.add(key)
            refs.append(r)

    return CanonicalBundle(nodes=nodes, edges=edges, refs=refs)
