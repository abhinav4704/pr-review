"""Allowlists for node labels and edge types.

Used to validate any label/relationship-type that gets interpolated into a
Cypher string (Cypher cannot parametrize labels/rel-types), preventing
injection. Keep in sync with ../ARCHITECTURE.md section 5.
"""
from __future__ import annotations

# Every node also carries the shared label CodeNode (single id index).
SHARED_LABEL = "CodeNode"

NODE_LABELS = {
    "Repository",   # the indexed repo root (one per repo)
    "Package",      # a package/namespace (Java package decl, Python dir path)
    "File",
    "Module",
    "Class",
    "Function",
    "Field",
    "Annotation",
    "Endpoint",     # an HTTP endpoint (method + route); in-repo or external
}

EDGE_TYPES = {
    "CONTAINS",
    "IMPORTS",
    "CALLS",
    "INSTANTIATES",
    "EXTENDS",
    "IMPLEMENTS",
    "ANNOTATED_WITH",
    # Milestone 2 — type system
    "RETURNS",      # Function -> Class (declared return type)
    "OF_TYPE",      # Field -> Class (declared type)
    "HAS_TYPE",     # Function -> Class (a parameter's type)
    "HAS_GENERIC",  # carrier -> Class (a generic type argument, e.g. List[User])
    # Milestone 4 — program relationships
    "OVERRIDES",    # Function -> Function (overrides/implements a base method) [SCIP]
    "READS",        # Function -> Field (reads instance/class state)
    "WRITES",       # Function -> Field (mutates instance/class state)
    "THROWS",       # Function -> Class (raises an exception type)
    "CATCHES",      # Function -> Class (catches an exception type)
    # HTTP-API layer
    "EXPOSES",      # Function -> Endpoint (backend handler serves this route)
    "CALLS_API",    # Function -> Endpoint (outbound HTTP call to this route)
}


def assert_label(label: str) -> str:
    if label not in NODE_LABELS:
        raise ValueError(f"unknown node label: {label!r}")
    return label


def assert_edge(rtype: str) -> str:
    if rtype not in EDGE_TYPES:
        raise ValueError(f"unknown edge type: {rtype!r}")
    return rtype
