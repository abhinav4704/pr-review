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

from .apispec import (
    endpoint_display,
    endpoint_fqn,
    endpoint_id,
    match_key,
    normalize_route,
)
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

    imports_by_file: dict[str, set[str]] = defaultdict(set)
    import_fqns_by_file: dict[str, set[str]] = defaultdict(set)
    for ref in refs:
        if ref.type != "IMPORTS" or not ref.ref_file:
            continue
        if ref.target_name:
            imports_by_file[ref.ref_file].add(_tail_name(ref.target_name))
        if ref.import_fqn:
            import_fqns_by_file[ref.ref_file].add(ref.import_fqn)

    # Endpoint index for CALLS_API matching. Exact key first; templated routes
    # (segments collapsed to `*`) matched segment-wise so a concrete caller path
    # `/api/users/42` resolves to the server's `/api/users/{id}`.
    endpoints_by_key: dict[tuple[str, str], list[Node]] = defaultdict(list)
    endpoint_patterns: dict[str, list[tuple[list[str], Node]]] = defaultdict(list)
    for n in nodes:
        if n.label != "Endpoint":
            continue
        method, route = match_key(n.method, n.route)
        endpoints_by_key[(method, route)].append(n)
        if "*" in route:
            endpoint_patterns[method].append((_route_segments(route), n))

    def match_endpoints(method: str, route: str) -> list[Node]:
        exact = endpoints_by_key.get((method, route))
        if exact:
            return exact
        caller = _route_segments(route)
        hits = []
        for segs, ep in endpoint_patterns.get(method, []):
            if len(segs) == len(caller) and all(
                ps == "*" or ps == cs for ps, cs in zip(segs, caller)
            ):
                hits.append(ep)
        return hits

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

    def narrow_call(ref: RawRef) -> tuple[list[Node], str]:
        """Best candidate set for a CALLS ref with deterministic strategy ordering."""
        name = ref.target_name
        base = by_name.get(name, [])
        pool = [c for c in base if c.label == "Function"] or base
        if not pool:
            return [], "none"
        src_node = nodes_by_id.get(ref.src)

        # (1) Same-scope preference: same immediate container (class/file/function).
        src_parent = parent_of.get(ref.src)
        if src_parent is not None:
            same_scope = [c for c in pool if parent_of.get(c.id) == src_parent]
            if same_scope:
                return _apply_arity(ref, same_scope, "same_scope")

        # (2) Same-file preference.
        if src_node is not None and src_node.file:
            same_file = [c for c in pool if c.file == src_node.file]
            if same_file:
                return _apply_arity(ref, same_file, "same_file")

        # (3) Import-aware narrowing by imported simple names and qualified imports.
        if src_node is not None and src_node.file:
            imported = imports_by_file.get(src_node.file, set())
            imported_fqns = import_fqns_by_file.get(src_node.file, set())
            qualified_hits = _import_qualified_hits(pool, imported_fqns)
            if qualified_hits:
                return _apply_arity(ref, qualified_hits, "imports_qualified")
            if imported:
                imported_hits = [
                    c for c in pool
                    if c.name in imported or _tail_name(c.fqn) in imported
                ]
                if imported_hits:
                    return _apply_arity(ref, imported_hits, "imports")

        # (4) Receiver-type narrowing.
        if ref.recv_type and ref.recv_type in classes_by_name:
            hits: list[Node] = []
            for ccls in classes_by_name[ref.recv_type]:
                hits.extend(methods_of_class[ccls.id].get(name, []))
            if hits:
                return _apply_arity(ref, hits, "receiver_type_hint")

        # self/cls dispatch -> a method of the enclosing class.
        if ref.recv in ("self", "cls"):
            cid = enclosing_class_id(ref.src)
            if cid is not None:
                m = methods_of_class[cid].get(name)
                if m:
                    return _apply_arity(ref, m, "receiver_type")

        # Receiver is an in-repo class name -> that class's methods.
        if ref.recv and ref.recv not in ("self", "cls") and ref.recv in classes_by_name:
            hits: list[Node] = []
            for ccls in classes_by_name[ref.recv]:
                hits.extend(methods_of_class[ccls.id].get(name, []))
            if hits:
                return _apply_arity(ref, hits, "receiver_type")

        # (5) Arity-only fallback if no stronger narrowing worked.
        return _apply_arity(ref, pool, "name")

    extra_nodes: list[Node] = []
    annotation_ids: dict[str, str] = {}
    event_ids: dict[str, str] = {}
    policy_ids: dict[str, str] = {}
    api_endpoint_ids: set[str] = set()
    out_edges: list[Edge] = []
    coverage: dict[str, Coverage] = defaultdict(Coverage)

    def make_edge(
        ref: RawRef,
        dst: str,
        confidence: str,
        strategy: str = "",
        edge_type: str | None = None,
    ) -> Edge:
        # Heuristic edges are still EXTRACTED-origin (the reference is observed in
        # source); the *resolution* uncertainty is carried by `confidence`.
        return Edge(
            edge_type or ref.type, ref.src, dst, confidence,
            origin=Origin.EXTRACTED.value, extractor="heuristic",
            evidence_file=ref.ref_file, evidence_line=ref.ref_line,
            evidence_col=ref.ref_col,
            strategy=strategy,
            arg_names=ref.arg_names,
        )

    def emit(ref: RawRef, cov: Coverage, wanted: list[Node], confidence: str,
             known_in_repo: bool, strategy: str = ""):
        if len(wanted) == 1:
            out_edges.append(make_edge(ref, wanted[0].id, confidence, strategy=strategy))
            cov.resolved += 1
        elif len(wanted) > 1:
            for c in wanted:
                out_edges.append(
                    make_edge(ref, c.id, Confidence.AMBIGUOUS.value, strategy=strategy)
                )
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
            out_edges.append(make_edge(ref, aid, Confidence.EXTRACTED.value, strategy="annotation"))
            cov.resolved += 1
            continue

        if ref.type in ("EMITS_EVENT", "CONSUMES_EVENT"):
            topic = _normalize_event_name(ref.target_name)
            if not topic:
                cov.unresolved += 1
                continue
            eid = event_ids.get(topic)
            if eid is None:
                eid = make_id(repo, f"event:{topic}", "event")
                event_ids[topic] = eid
                extra_nodes.append(Node(
                    id=eid, label="Event", name=topic,
                    fqn=f"event://{topic}", repo=repo, kind="event",
                ))
            out_edges.append(make_edge(ref, eid, Confidence.EXTRACTED.value, strategy="event_marker"))
            cov.resolved += 1
            continue

        if ref.type in ("REQUIRES_AUTH", "ENFORCES_POLICY"):
            pname = _normalize_policy_name(ref.target_name)
            if not pname:
                pname = "AUTH_REQUIRED" if ref.type == "REQUIRES_AUTH" else "POLICY"
            pid = policy_ids.get(pname)
            if pid is None:
                pid = make_id(repo, f"policy:{pname}", "policy")
                policy_ids[pname] = pid
                extra_nodes.append(Node(
                    id=pid, label="Policy", name=pname,
                    fqn=f"policy://{pname}", repo=repo, kind="policy",
                ))
            out_edges.append(make_edge(ref, pid, Confidence.EXTRACTED.value, strategy="auth_marker"))
            cov.resolved += 1
            continue

        if ref.type == "CALLS_API":
            method = (ref.http_method or "GET").upper()
            route = normalize_route(ref.target_name)
            host = ref.recv  # carries the external host ('' for a relative URL)
            hits = match_endpoints(method, route)
            if hits:
                # An in-repo backend exposes this route (resolves cross-file).
                for ep in hits:
                    out_edges.append(
                        make_edge(ref, ep.id, Confidence.EXTRACTED.value, strategy="api_match")
                    )
                cov.resolved += 1
            else:
                # No in-repo handler: synthesize the target so the edge lands.
                # External host -> external Endpoint (shared id across repos);
                # relative path -> an unresolved in-repo Endpoint (a likely dead/
                # missing route worth surfacing).
                if host:
                    eid = endpoint_id("external", method, route, host)
                    erepo, conf, strat = "external", Confidence.EXTRACTED.value, "api_external"
                else:
                    eid = endpoint_id(repo, method, route)
                    erepo, conf, strat = repo, Confidence.INFERRED.value, "api_unresolved"
                if eid not in api_endpoint_ids:
                    api_endpoint_ids.add(eid)
                    extra_nodes.append(Node(
                        id=eid, label="Endpoint",
                        name=endpoint_display(method, route, host),
                        fqn=endpoint_fqn(method, route, host),
                        repo=erepo, kind="endpoint",
                        method=method, route=route, host=host,
                    ))
                out_edges.append(make_edge(ref, eid, conf, strategy=strat))
                cov.resolved += 1
            continue

        if ref.type in ("CALLS", "PASSES"):
            wanted, strategy = narrow_call(ref)
            # Precision guard: a call on an *unknown* receiver that matched only by
            # global name (no scope/file/import/receiver-type evidence) is not a
            # trustworthy CALLS — it likely targets an external object that merely
            # shares a method name. Demote it to a weak REFERENCES symbol-use edge
            # instead of asserting a precise call. Bare calls (no receiver) and the
            # PASSES shadow are left untouched.
            if (
                ref.type == "CALLS"
                and wanted
                and strategy.startswith("name")
                and ref.recv
                and ref.recv not in ("self", "cls")
            ):
                cov.total -= 1  # this site is accounted under REFERENCES, not CALLS
                rcov = coverage["REFERENCES"]
                rcov.total += 1
                if len(wanted) == 1:
                    out_edges.append(make_edge(
                        ref, wanted[0].id, Confidence.INFERRED.value,
                        strategy=f"{strategy}+unknown_recv", edge_type="REFERENCES",
                    ))
                    rcov.resolved += 1
                else:
                    for c in wanted:
                        out_edges.append(make_edge(
                            ref, c.id, Confidence.AMBIGUOUS.value,
                            strategy=f"{strategy}+unknown_recv", edge_type="REFERENCES",
                        ))
                    rcov.ambiguous += 1
                continue
            emit(
                ref,
                cov,
                wanted,
                Confidence.INFERRED.value,
                known_in_repo=ref.target_name in by_name,
                strategy=strategy,
            )
            if not wanted:
                # Fallback: keep recall via a weaker symbol-use edge when a
                # call target can't be resolved as a CALLS/PASSES destination.
                fallback = _fallback_reference_candidates(ref.target_name, by_name)
                if fallback:
                    rcov = coverage["REFERENCES"]
                    rcov.total += 1
                    if len(fallback) == 1:
                        out_edges.append(
                            make_edge(
                                ref,
                                fallback[0].id,
                                Confidence.INFERRED.value,
                                strategy=f"{strategy or 'none'}+fallback_tail",
                                edge_type="REFERENCES",
                            )
                        )
                        rcov.resolved += 1
                    else:
                        for c in fallback:
                            out_edges.append(
                                make_edge(
                                    ref,
                                    c.id,
                                    Confidence.AMBIGUOUS.value,
                                    strategy=f"{strategy or 'none'}+fallback_tail",
                                    edge_type="REFERENCES",
                                )
                            )
                        rcov.ambiguous += 1
            continue

        if ref.type in ("READS", "WRITES"):
            # self.<field> resolved to the enclosing class's field — scope-exact.
            cid = enclosing_class_id(ref.src)
            wanted = fields_of_class[cid].get(ref.target_name, []) if cid else []
            emit(
                ref,
                cov,
                wanted,
                Confidence.EXTRACTED.value,
                known_in_repo=bool(wanted),
                strategy="same_scope",
            )
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
        emit(
            ref,
            cov,
            wanted,
            Confidence.INFERRED.value,
            known_in_repo=ref.target_name in by_name,
            strategy="kind_hint",
        )

    return extra_nodes, out_edges, coverage


def _route_segments(route: str) -> list[str]:
    return [s for s in route.split("/") if s]


def _tail_name(name: str) -> str:
    if not name:
        return ""
    return name.split(".")[-1]


def _apply_arity(ref: RawRef, candidates: list[Node], base_strategy: str) -> tuple[list[Node], str]:
    if ref.call_arity < 0:
        return candidates, base_strategy
    narrowed = [c for c in candidates if c.param_count == ref.call_arity]
    if narrowed:
        return narrowed, f"{base_strategy}+arity"
    return candidates, base_strategy


def _import_qualified_hits(candidates: list[Node], imported_fqns: set[str]) -> list[Node]:
    if not imported_fqns:
        return []
    hits: list[Node] = []
    for c in candidates:
        fqn = c.fqn or ""
        for imp in imported_fqns:
            tail = _tail_name(imp)
            if not tail:
                continue
            # Deterministic prefix/namespace match: imported type/module appears
            # in candidate FQN path.
            if f".{tail}." in fqn or fqn.endswith(f".{tail}"):
                hits.append(c)
                break
    return hits


def _fallback_reference_candidates(
    target_name: str,
    by_name: dict[str, list[Node]],
) -> list[Node]:
    """Deterministic weak fallback for unresolved call-like refs.

    If a dotted callee token doesn't resolve directly (e.g. pkg.mod.fn),
    attempt tail-name matching and emit REFERENCES instead of dropping signal.
    """
    tail = _tail_name(target_name)
    if not tail or tail == target_name:
        return []
    cands = by_name.get(tail, [])
    if not cands:
        return []
    fns = [c for c in cands if c.label == "Function"]
    return fns or cands


def _normalize_event_name(name: str) -> str:
    n = (name or "").strip().strip("\"'")
    if not n:
        return ""
    return n


def _normalize_policy_name(name: str) -> str:
    n = (name or "").strip().strip("\"'")
    if not n:
        return ""
    return n.upper()
