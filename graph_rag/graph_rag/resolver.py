"""Stage 2 (Phase-0 heuristic) — resolve name-based RawRefs to edges.

A real call graph needs scip-java/Pyright. This first pass resolves by name
within the repo, but uses cheap lexical scope to cut ambiguity for Python:

  (a) `self.method()` / `cls.method()` → methods of the enclosing class.
  (b) `Class.method()`                 → methods of that in-repo class.
  (c) bare `func()`                    → prefer a same-file definition before
                                         falling back to the global name index.

Unique match -> INFERRED edge; multiple -> AMBIGUOUS edges to all candidates;
no match -> unresolved (counted, no edge). Annotations are materialized as
:Annotation nodes keyed by name (EXTRACTED: the usage is observed directly).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .ids import make_id
from .models import Confidence, Edge, Node, Origin, RawRef


@dataclass
class Coverage:
    total: int = 0
    resolved: int = 0     # unique match
    ambiguous: int = 0    # >1 candidate
    unresolved: int = 0   # 0 candidates but the name DOES exist in-repo (a real miss)
    external: int = 0     # 0 candidates and the name is unknown in-repo (stdlib/3rd-party/builtin)

    @property
    def inrepo(self) -> int:
        """Refs that target something nameable in this repo (excludes external)."""
        return self.resolved + self.ambiguous + self.unresolved

    def pct(self) -> float:
        """Honest resolution rate: resolved as a share of in-repo targets."""
        return 100.0 * self.resolved / self.inrepo if self.inrepo else 0.0


def resolve(nodes: list[Node], edges: list[Edge], refs: list[RawRef], repo: str):
    """Return (extra_nodes, edges, coverage_by_reftype).

    `edges` is the structural edge list from extraction (CONTAINS), used to
    build the containment scope that powers scope-aware call resolution.
    """
    nodes_by_id: dict[str, Node] = {n.id: n for n in nodes}

    by_name: dict[str, list[Node]] = defaultdict(list)
    classes_by_name: dict[str, list[Node]] = defaultdict(list)
    for n in nodes:
        if n.label in ("Class", "Function"):
            by_name[n.name].append(n)
        if n.label == "Class":
            classes_by_name[n.name].append(n)

    # Containment: child -> parent, and class -> {method_name: [nodes]}.
    parent_of: dict[str, str] = {}
    for e in edges:
        if e.type == "CONTAINS":
            parent_of[e.dst] = e.src

    methods_of_class: dict[str, dict[str, list[Node]]] = defaultdict(lambda: defaultdict(list))
    fields_of_class: dict[str, dict[str, list[Node]]] = defaultdict(lambda: defaultdict(list))
    for n in nodes:
        p = parent_of.get(n.id)
        if not (p and nodes_by_id.get(p) and nodes_by_id[p].label == "Class"):
            continue
        if n.label == "Function" and n.kind == "method":
            methods_of_class[p][n.name].append(n)
        elif n.label == "Field":
            fields_of_class[p][n.name].append(n)

    def enclosing_class_id(node_id: str) -> str | None:
        cur = parent_of.get(node_id)
        while cur is not None:
            cn = nodes_by_id.get(cur)
            if cn is None:
                break
            if cn.label == "Class":
                return cur
            cur = parent_of.get(cur)
        return None

    def narrow_call(ref: RawRef) -> list[Node]:
        """Best candidate set for a CALLS ref, narrowed by lexical scope."""
        name = ref.target_name
        base = by_name.get(name, [])
        pool = [c for c in base if c.label == "Function"] or base
        if not pool:
            return []
        src_node = nodes_by_id.get(ref.src)

        # (a) self/cls dispatch -> a method of the enclosing class.
        if ref.recv in ("self", "cls"):
            cid = enclosing_class_id(ref.src)
            if cid is not None:
                m = methods_of_class[cid].get(name)
                if m:
                    return m

        # (b) Receiver is an in-repo class name -> that class's methods
        #     (covers `Foo.bar()` static/factory style calls).
        if ref.recv and ref.recv not in ("self", "cls") and ref.recv in classes_by_name:
            hits: list[Node] = []
            for ccls in classes_by_name[ref.recv]:
                hits.extend(methods_of_class[ccls.id].get(name, []))
            if hits:
                return hits

        # (c) Module-local preference: a same-file definition wins over
        #     identically-named functions elsewhere in the repo.
        if src_node is not None and src_node.file:
            local = [c for c in pool if c.file == src_node.file]
            if local:
                return local
        return pool

    extra_nodes: list[Node] = []
    annotation_ids: dict[str, str] = {}
    out_edges: list[Edge] = []
    coverage: dict[str, Coverage] = defaultdict(Coverage)

    def make_edge(ref: RawRef, dst: str, confidence: str) -> Edge:
        # Heuristic edges are still EXTRACTED-origin (the reference is observed in
        # source); the *resolution* uncertainty is carried by `confidence`.
        return Edge(
            ref.type, ref.src, dst, confidence,
            origin=Origin.EXTRACTED.value, extractor="heuristic",
            evidence_file=ref.ref_file, evidence_line=ref.ref_line,
            evidence_col=ref.ref_col,
        )

    def emit(ref: RawRef, cov: Coverage, wanted: list[Node], confidence: str,
             known_in_repo: bool):
        if len(wanted) == 1:
            out_edges.append(make_edge(ref, wanted[0].id, confidence))
            cov.resolved += 1
        elif len(wanted) > 1:
            for c in wanted:
                out_edges.append(make_edge(ref, c.id, Confidence.AMBIGUOUS.value))
            cov.ambiguous += 1
        elif known_in_repo:
            cov.unresolved += 1   # the name exists in-repo but scope didn't pin it
        else:
            cov.external += 1     # nothing by that name here -> stdlib/3rd-party/builtin

    for ref in refs:
        cov = coverage[ref.type]
        cov.total += 1

        if ref.type == "ANNOTATED_WITH":
            aid = annotation_ids.get(ref.target_name)
            if aid is None:
                aid = make_id(repo, f"@{ref.target_name}", "annotation")
                annotation_ids[ref.target_name] = aid
                extra_nodes.append(Node(
                    id=aid, label="Annotation", name=ref.target_name,
                    fqn=f"@{ref.target_name}", repo=repo, kind="annotation",
                ))
            out_edges.append(make_edge(ref, aid, Confidence.EXTRACTED.value))
            cov.resolved += 1
            continue

        if ref.type == "CALLS":
            emit(ref, cov, narrow_call(ref), Confidence.INFERRED.value,
                 known_in_repo=ref.target_name in by_name)
            continue

        if ref.type in ("READS", "WRITES"):
            # self.<field> resolved to the enclosing class's field — scope-exact.
            cid = enclosing_class_id(ref.src)
            wanted = fields_of_class[cid].get(ref.target_name, []) if cid else []
            emit(ref, cov, wanted, Confidence.EXTRACTED.value, known_in_repo=bool(wanted))
            continue

        candidates = by_name.get(ref.target_name, [])
        # type-shaped refs should resolve to classes; call-shaped to functions
        if ref.kind_hint in ("type", "import"):
            wanted = [c for c in candidates if c.label == "Class"]
        elif ref.kind_hint == "call":
            wanted = [c for c in candidates if c.label == "Function"]
        else:
            wanted = candidates
        wanted = wanted or candidates
        emit(ref, cov, wanted, Confidence.INFERRED.value,
             known_in_repo=ref.target_name in by_name)

    return extra_nodes, out_edges, coverage
