"""Phase 0 orchestration: discover -> extract -> resolve -> write."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import extractors
from .discovery import discover
from .models import Edge, Node, RawRef
from .resolver import Coverage, resolve
from .scip_resolver import ScipReport, scip_resolve
from .store import GraphStore


@dataclass
class IndexResult:
    repo: str
    files: int = 0
    nodes: int = 0
    edges: int = 0
    seconds: float = 0.0
    coverage: dict[str, Coverage] = field(default_factory=dict)
    db_counts: dict = field(default_factory=dict)
    scip: ScipReport = field(default_factory=ScipReport)


def index_repo(root: str, repo: str, store: GraphStore, wipe: bool = True,
               scip: bool = True) -> IndexResult:
    t0 = time.time()
    files = discover(root)

    all_nodes: list[Node] = []
    all_edges: list[Edge] = []
    all_refs: list[RawRef] = []

    for f in files:
        nodes, edges, refs = extractors.extract(f, repo)
        all_nodes.extend(nodes)
        all_edges.extend(edges)
        all_refs.extend(refs)

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
        db_counts=store.counts(repo),
        scip=scip_report,
    )
