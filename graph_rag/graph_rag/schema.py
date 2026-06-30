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
    "Event",        # an event/topic/queue semantic node
    "Policy",       # auth/policy contract node (role/scope/policy marker)
}

EDGE_TYPES = {
    "CONTAINS",
    "BELONGS_TO",  # node -> Module ownership relation
    "DEFINES",      # semantic ownership/provenance (file/class defines symbol)
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
    # Flexible dependency layer (additive, lower-trust by default)
    "REFERENCES",   # generic symbol use when stricter typing is unavailable
    "USES",         # higher-level/component dependency
    "PASSES",       # argument/data propagation hint (lightweight, not full DFG)
    "AUTOWIRED",    # dependency-injection wiring relation
    "RE_EXPORTS",   # symbol forwarding/export indirection (mainly JS/TS ecosystems)
    # Event/auth layer
    "EMITS_EVENT",      # Function -> Event (publishes to topic/queue)
    "CONSUMES_EVENT",   # Function -> Event (subscribes/listens to topic/queue)
    "REQUIRES_AUTH",    # Function/Class -> Policy (auth requirement)
    "ENFORCES_POLICY",  # Function/Class -> Policy (authorization rule)
}


def assert_label(label: str) -> str:
    if label not in NODE_LABELS:
        raise ValueError(f"unknown node label: {label!r}")
    return label


def assert_edge(rtype: str) -> str:
    if rtype not in EDGE_TYPES:
        raise ValueError(f"unknown edge type: {rtype!r}")
    return rtype
