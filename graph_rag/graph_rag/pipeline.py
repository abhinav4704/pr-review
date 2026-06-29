"""Phase 0 orchestration: discover -> extract -> resolve -> write."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import extractors
from .canonical_ir import from_extractor, merge_bundles
from .discovery import discover
from .models import Edge, Node, Origin, RawRef
from .resolver import Coverage, resolve
from .scip_resolver import ScipReport, scip_resolve
from .store import GraphStore
from .validator import validate_graph


_TYPE_RELATIONS = {"RETURNS", "OF_TYPE", "HAS_TYPE", "HAS_GENERIC"}


@dataclass
class IndexResult:
    repo: str
    files: int = 0
    nodes: int = 0
    edges: int = 0
    seconds: float = 0.0
    coverage: dict[str, Coverage] = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    db_counts: dict = field(default_factory=dict)
    scip: ScipReport = field(default_factory=ScipReport)


def index_repo(root: str, repo: str, store: GraphStore, wipe: bool = True,
               scip: bool = True) -> IndexResult:
    t0 = time.time()
    files = discover(root)

    all_nodes: list[Node] = []
    all_edges: list[Edge] = []
    all_refs: list[RawRef] = []
    canonical_bundles = []

    for f in files:
        nodes, edges, refs = extractors.extract(f, repo)
        canonical_bundles.append(from_extractor(nodes, edges, refs))

    canonical = merge_bundles(canonical_bundles)
    all_nodes.extend(canonical.nodes)
    all_edges.extend(canonical.edges)
    all_refs.extend(canonical.refs)

    extra_nodes, resolved_edges, coverage = resolve(all_nodes, all_edges, all_refs, repo)
    all_nodes.extend(extra_nodes)
    all_edges.extend(resolved_edges)

    # Stage 2 (precise): let SCIP own Python CALLS — type-precise and cross-file.
    # The heuristic keeps non-Python CALLS (e.g. Java, where scip-java needs a
    # build tool) plus every other edge type.
    scip_report = ScipReport()
    has_python = any(f.lang == "python" for f in files)
    if scip and has_python:
        scip_edges, scip_report = scip_resolve(all_nodes, root, repo)
        if scip_report.available:
            lang_of = {n.id: n.lang for n in all_nodes}
            all_edges = [
                e for e in all_edges
                if not (e.type == "CALLS" and lang_of.get(e.src) == "python")
            ]
            all_edges.extend(scip_edges)
            coverage.pop("CALLS", None)  # superseded by SCIP for Python

    all_edges.extend(_derive_deterministic_edges(all_nodes, all_edges))
    _attach_call_metrics(all_nodes, all_edges)
    validation = validate_graph(all_nodes, all_edges)

    store.bootstrap()
    if wipe:
        store.wipe(repo)
    store.write_nodes(all_nodes)
    store.write_edges(all_edges)

    return IndexResult(
        repo=repo,
        files=len(files),
        nodes=len(all_nodes),
        edges=len(all_edges),
        seconds=time.time() - t0,
        coverage=dict(coverage),
        validation=validation,
        db_counts=store.counts(repo),
        scip=scip_report,
    )


def _derive_deterministic_edges(nodes: list[Node], edges: list[Edge]) -> list[Edge]:
    nodes_by_id = {n.id: n for n in nodes}
    out: list[Edge] = []
    seen: set[tuple[str, str, str]] = {(e.type, e.src, e.dst) for e in edges}

    for e in edges:
        key = ("DECLARES", e.src, e.dst)
        if e.type == "CONTAINS" and key not in seen:
            src = nodes_by_id.get(e.src)
            dst = nodes_by_id.get(e.dst)
            if src and dst and src.label in ("File", "Module", "Class"):
                out.append(
                    Edge(
                        "DECLARES",
                        e.src,
                        e.dst,
                        confidence=e.confidence,
                        origin=Origin.DERIVED.value,
                        extractor="deterministic",
                        evidence_file=e.evidence_file,
                        evidence_line=e.evidence_line,
                        evidence_col=e.evidence_col,
                        strategy="contains_alias",
                    )
                )
                seen.add(key)

        key = ("USES_TYPE", e.src, e.dst)
        if e.type in _TYPE_RELATIONS and key not in seen:
            out.append(
                Edge(
                    "USES_TYPE",
                    e.src,
                    e.dst,
                    confidence=e.confidence,
                    origin=Origin.DERIVED.value,
                    extractor="deterministic",
                    evidence_file=e.evidence_file,
                    evidence_line=e.evidence_line,
                    evidence_col=e.evidence_col,
                    strategy="type_alias",
                )
            )
            seen.add(key)

    return out


def _attach_call_metrics(nodes: list[Node], edges: list[Edge]) -> None:
    nodes_by_id = {n.id: n for n in nodes}
    fan_out: dict[str, int] = {}
    fan_in: dict[str, int] = {}
    recursive: set[str] = set()

    for e in edges:
        if e.type != "CALLS":
            continue
        fan_out[e.src] = fan_out.get(e.src, 0) + 1
        fan_in[e.dst] = fan_in.get(e.dst, 0) + 1
        if e.src == e.dst:
            recursive.add(e.src)

    for node_id, n in nodes_by_id.items():
        if n.label != "Function":
            continue
        n.fan_out = fan_out.get(node_id, 0)
        n.fan_in = fan_in.get(node_id, 0)
        n.recursive = node_id in recursive
