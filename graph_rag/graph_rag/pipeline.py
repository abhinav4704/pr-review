"""Phase 0 orchestration: discover -> extract -> resolve -> write."""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

from . import extractors
from .canonical_ir import from_extractor, merge_bundles
from .discovery import discover
from .ids import make_id
from .models import Confidence, Edge, Node, Origin, RawRef
from .resolver import Coverage, resolve
from .scip_resolver import ScipReport, scip_resolve
from .store import GraphStore
from .validator import validate_graph


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
    lang_of = {n.id: n.lang for n in all_nodes}
    scip_owns_python = False
    if scip and has_python:
        scip_edges, scip_report = scip_resolve(all_nodes, root, repo)
        if scip_report.available:
            scip_owns_python = True
            all_edges = [
                e for e in all_edges
                if not (e.type == "CALLS" and lang_of.get(e.src) == "python")
            ]
            all_edges.extend(scip_edges)
            coverage.pop("CALLS", None)  # superseded by SCIP for Python

    # Deterministic OVERRIDES from the class hierarchy (Java + Python). SCIP gives
    # precise Python overrides when available, so drop heuristic Python ones then.
    override_edges = _derive_overrides(all_nodes, all_edges)
    if scip_owns_python:
        override_edges = [e for e in override_edges if lang_of.get(e.src) != "python"]
    all_edges.extend(override_edges)

    pkg_nodes, pkg_edges = _build_package_tree(all_nodes, repo)
    all_nodes.extend(pkg_nodes)
    all_edges.extend(pkg_edges)

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


def _derive_overrides(nodes: list[Node], edges: list[Edge]) -> list[Edge]:
    """OVERRIDES from the resolved class hierarchy: a method overrides an
    ancestor method of the same name + arity (Java extends/implements, Python
    inheritance). Confidence INFERRED — name+arity match, not type-precise."""
    by_id = {n.id: n for n in nodes}
    parent_of: dict[str, str] = {}
    supers: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e.type == "CONTAINS":
            parent_of[e.dst] = e.src
        elif e.type in ("EXTENDS", "IMPLEMENTS"):
            s, d = by_id.get(e.src), by_id.get(e.dst)
            if s and d and s.label == "Class" and d.label == "Class":
                supers[e.src].append(e.dst)

    methods_of: dict[str, dict[str, list[Node]]] = defaultdict(lambda: defaultdict(list))
    for n in nodes:
        if n.label == "Function" and n.kind == "method":
            cls = parent_of.get(n.id)
            if cls and by_id.get(cls) and by_id[cls].label == "Class":
                methods_of[cls][n.name].append(n)

    out: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for n in nodes:
        if n.label != "Function" or n.kind != "method":
            continue
        cls = parent_of.get(n.id)
        if cls is None:
            continue
        # BFS up the ancestor chain (multiple inheritance / interfaces).
        visited: set[str] = set()
        stack = list(supers.get(cls, []))
        while stack:
            anc = stack.pop()
            if anc in visited:
                continue
            visited.add(anc)
            for m in methods_of.get(anc, {}).get(n.name, []):
                if m.id != n.id and m.param_count == n.param_count:
                    key = (n.id, m.id)
                    if key not in seen:
                        seen.add(key)
                        out.append(Edge(
                            "OVERRIDES", n.id, m.id,
                            confidence=Confidence.INFERRED.value,
                            origin=Origin.EXTRACTED.value,
                            extractor="heuristic", strategy="hierarchy",
                            evidence_file=n.file, evidence_line=n.start_line,
                        ))
            stack.extend(supers.get(anc, []))
    return out


def _build_package_tree(nodes: list[Node], repo: str) -> tuple[list[Node], list[Edge]]:
    """Materialize Repository -> Package* -> File containment from each File's
    package (Java package decl / Python directory path). Built once over all
    files so package/repo CONTAINS edges are emitted exactly once."""
    new_nodes: list[Node] = []
    new_edges: list[Edge] = []
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str]] = set()

    def add_node(nid, label, name, fqn, **kw):
        if nid in seen_nodes:
            return
        seen_nodes.add(nid)
        new_nodes.append(Node(id=nid, label=label, name=name, fqn=fqn, repo=repo, **kw))

    def add_contains(src_id, dst_id):
        if (src_id, dst_id) in seen_edges:
            return
        seen_edges.add((src_id, dst_id))
        new_edges.append(Edge(
            "CONTAINS", src_id, dst_id,
            origin=Origin.EXTRACTED.value, extractor="structure",
        ))

    repo_id = make_id(repo, repo, "repository")
    add_node(repo_id, "Repository", repo, repo, kind="repository")

    for n in nodes:
        if n.label != "File":
            continue
        parent_id = repo_id
        if n.package:
            parts = n.package.split(".")
            for i in range(len(parts)):
                fqn = ".".join(parts[:i + 1])
                pid = make_id(repo, fqn, "package")
                add_node(pid, "Package", parts[i], fqn,
                         kind="package", package=fqn, lang=n.lang)
                add_contains(parent_id, pid)
                parent_id = pid
        add_contains(parent_id, n.id)

    return new_nodes, new_edges


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
