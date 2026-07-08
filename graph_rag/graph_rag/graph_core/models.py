"""Core data structures emitted by extractors and consumed by the resolver/store."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Confidence(str, Enum):
    EXTRACTED = "EXTRACTED"   # observed directly in syntax / resolved precisely
    INFERRED = "INFERRED"     # resolved by heuristic (name match)
    AMBIGUOUS = "AMBIGUOUS"   # multiple/uncertain resolution


class Origin(str, Enum):
    """How a fact entered the graph — orthogonal to Confidence.

    EXTRACTED = read off the AST / a language index (tree-sitter, SCIP).
    DERIVED   = computed by later analysis over the graph (call-graph closure,
                communities, blast-radius materialization, …).
    """
    EXTRACTED = "EXTRACTED"
    DERIVED = "DERIVED"


def _clean(d: dict) -> dict:
    """Drop empty values so we never write meaningless props to Neo4j."""
    out = {}
    for k, v in d.items():
        if v in ("", 0, None, False):
            continue
        if isinstance(v, (list, tuple, dict)) and not v:
            continue
        out[k] = list(v) if isinstance(v, tuple) else v
    return out


@dataclass
class Node:
    id: str
    label: str          # File | Module | Class | Function | Field | Annotation
    name: str
    fqn: str
    repo: str
    kind: str = ""      # class|interface|enum|record / method|constructor|function|lambda
    lang: str = ""
    file: str = ""
    package: str = ""   # owning package/namespace fqn (File: its package)
    # source range (1-based line, 0-based column — matches tree-sitter points)
    start_line: int = 0
    start_col: int = 0
    end_line: int = 0
    end_col: int = 0
    # structural metadata (Milestone 1)
    display_name: str = ""
    visibility: str = ""              # public|private|protected|package
    modifiers: list[str] = field(default_factory=list)
    is_static: bool = False
    is_abstract: bool = False
    is_async: bool = False
    return_type: str = ""
    param_count: int = 0
    param_names: list[str] = field(default_factory=list)   # input parameter names (ordered)
    param_types: list[str] = field(default_factory=list)   # declared types aligned to param_names ("" if untyped)
    signature: str = ""
    docstring: str = ""
    body_hash: str = ""
    # HTTP-API metadata (Endpoint nodes)
    method: str = ""                  # GET|POST|... (Endpoint)
    route: str = ""                   # normalized URL path (Endpoint)
    host: str = ""                    # external host, e.g. api.stripe.com ("" = in-repo)
    # static metrics (M5)
    loc: int = 0
    cyclomatic: int = 0
    branch_count: int = 0
    loop_count: int = 0
    fan_in: int = 0
    fan_out: int = 0
    recursive: bool = False
    # derived architecture metadata
    component_role: str = ""          # controller|service|repository|entity|config|util|...
    role_source: str = ""             # annotation|name_suffix|package|fallback
    role_confidence: str = ""         # HIGH|MEDIUM|LOW
    module_id: str = ""               # owning Module node id (derived)
    # Field-node-only metadata
    scope: str = ""                   # Field: class|module — where the variable lives
    is_lock: bool = False             # Field: True if assigned a Lock/RLock/Semaphore/Condition
    # provenance
    extractor: str = ""              # who produced this node (tree-sitter)
    confidence: str = Confidence.EXTRACTED.value

    def props(self) -> dict:
        """Property map written to Neo4j (everything except id, set via MERGE)."""
        return _clean({
            "name": self.name,
            "fqn": self.fqn,
            "repo": self.repo,
            "kind": self.kind,
            "lang": self.lang,
            "file": self.file,
            "package": self.package,
            "start_line": self.start_line,
            "start_col": self.start_col,
            "end_line": self.end_line,
            "end_col": self.end_col,
            "display_name": self.display_name,
            "visibility": self.visibility,
            "modifiers": self.modifiers,
            "is_static": self.is_static,
            "is_abstract": self.is_abstract,
            "is_async": self.is_async,
            "return_type": self.return_type,
            "param_count": self.param_count,
            "param_names": self.param_names,
            "param_types": self.param_types,
            "signature": self.signature,
            "docstring": self.docstring,
            "body_hash": self.body_hash,
            "method": self.method,
            "route": self.route,
            "host": self.host,
            "loc": self.loc,
            "cyclomatic": self.cyclomatic,
            "branch_count": self.branch_count,
            "loop_count": self.loop_count,
            "fan_in": self.fan_in,
            "fan_out": self.fan_out,
            "recursive": self.recursive,
            "component_role": self.component_role,
            "role_source": self.role_source,
            "role_confidence": self.role_confidence,
            "module_id": self.module_id,
            "scope": self.scope,
            "is_lock": self.is_lock,
            "extractor": self.extractor,
            "confidence": self.confidence,
        })


@dataclass
class Edge:
    type: str
    src: str            # source node id (resolved)
    dst: str            # destination node id (resolved)
    confidence: str = Confidence.EXTRACTED.value
    # provenance (Milestone 1)
    origin: str = Origin.EXTRACTED.value
    extractor: str = ""              # tree-sitter | scip-python | heuristic
    evidence_file: str = ""          # where the evidence for this edge lives
    evidence_line: int = 0           # 1-based
    evidence_col: int = 0            # 0-based
    strategy: str = ""              # resolver strategy used for destination selection
    arg_names: list[str] = field(default_factory=list)  # lightweight arg-flow payload (PASSES)

    def props(self) -> dict:
        return _clean({
            "confidence": self.confidence,
            "origin": self.origin,
            "extractor": self.extractor,
            "evidence_file": self.evidence_file,
            "evidence_line": self.evidence_line,
            "evidence_col": self.evidence_col,
            "strategy": self.strategy,
            "arg_names": self.arg_names,
        })


@dataclass
class RawRef:
    """An edge whose destination is only known by name; resolved later."""
    type: str
    src: str            # source node id (already resolved)
    target_name: str    # symbol name to resolve against the repo symbol index
    kind_hint: str = "" # 'call' | 'type' | 'import' | 'annotation'
    recv: str = ""      # call receiver tail: 'self'/'cls', a module/class/var name, or '' for a bare call
    recv_type: str = "" # inferred receiver class/type name when statically available
    import_fqn: str = "" # fully-qualified import path when available
    http_method: str = "" # HTTP verb for a CALLS_API ref (GET|POST|...); recv carries the host
    # location of the reference site (for edge provenance)
    ref_file: str = ""
    ref_line: int = 0   # 1-based
    ref_col: int = 0    # 0-based
    call_arity: int = -1
    arg_names: list[str] = field(default_factory=list)  # optional arg names for PASSES
