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
    signature: str = ""
    docstring: str = ""
    body_hash: str = ""
    # static metrics (M5)
    loc: int = 0
    cyclomatic: int = 0
    branch_count: int = 0
    loop_count: int = 0
    fan_in: int = 0
    fan_out: int = 0
    recursive: bool = False
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
            "signature": self.signature,
            "docstring": self.docstring,
            "body_hash": self.body_hash,
            "loc": self.loc,
            "cyclomatic": self.cyclomatic,
            "branch_count": self.branch_count,
            "loop_count": self.loop_count,
            "fan_in": self.fan_in,
            "fan_out": self.fan_out,
            "recursive": self.recursive,
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

    def props(self) -> dict:
        return _clean({
            "confidence": self.confidence,
            "origin": self.origin,
            "extractor": self.extractor,
            "evidence_file": self.evidence_file,
            "evidence_line": self.evidence_line,
            "evidence_col": self.evidence_col,
            "strategy": self.strategy,
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
    # location of the reference site (for edge provenance)
    ref_file: str = ""
    ref_line: int = 0   # 1-based
    ref_col: int = 0    # 0-based
    call_arity: int = -1
