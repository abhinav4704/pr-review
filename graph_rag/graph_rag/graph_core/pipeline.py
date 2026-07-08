"""Phase 0 orchestration: discover -> extract -> resolve -> write."""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import extractors
from .canonical_ir import from_extractor, merge_bundles
from .discovery import discover
from .ids import make_id
from .models import Confidence, Edge, Node, Origin, RawRef
from .resolver import Coverage, resolve
from .scip_resolver import ScipReport, scip_resolve, scip_resolve_java
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
    scip_java: ScipReport = field(default_factory=ScipReport)
    roles: dict = field(default_factory=dict)  # (role, source) -> count diagnostics


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

    # Same idea for Java via scip-java, gated on an actual Maven/Gradle build
    # being present (scip-java compiles the project with a semanticdb-javac
    # plugin to get type info, so it can't run without one). Falls back to the
    # (package/import-aware) heuristic resolver on any failure — missing
    # binary, no JDK, no network to fetch deps, compile errors, etc.
    scip_java_report = ScipReport()
    has_java = any(f.lang == "java" for f in files)
    scip_owns_java = False
    if scip and has_java:
        scip_java_edges, scip_java_report = scip_resolve_java(all_nodes, root, repo)
        if scip_java_report.available:
            scip_owns_java = True
            all_edges = [
                e for e in all_edges
                if not (e.type == "CALLS" and lang_of.get(e.src) == "java")
            ]
            all_edges.extend(scip_java_edges)
            coverage.pop("CALLS", None)  # superseded by SCIP for Java (and/or Python above)

    # Deterministic OVERRIDES from the class hierarchy (Java + Python). SCIP gives
    # precise overrides when available for a language, so drop heuristic ones then.
    override_edges = _derive_overrides(all_nodes, all_edges)
    if scip_owns_python:
        override_edges = [e for e in override_edges if lang_of.get(e.src) != "python"]
    if scip_owns_java:
        override_edges = [e for e in override_edges if lang_of.get(e.src) != "java"]
    all_edges.extend(override_edges)

    pkg_nodes, pkg_edges = _build_package_tree(all_nodes, repo)
    all_nodes.extend(pkg_nodes)
    all_edges.extend(pkg_edges)

    role_diag = _classify_roles(all_nodes, all_edges)
    mod_nodes, mod_edges, uses_edges = _derive_module_ownership_and_uses(all_nodes, all_edges, repo)
    all_nodes.extend(mod_nodes)
    all_edges.extend(mod_edges)
    all_edges.extend(uses_edges)

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
        scip_java=scip_java_report,
        roles={f"{role}:{source}": c for (role, source), c in role_diag.items()},
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


# Class component-role rules, highest precedence first. Each rule is
# (match_kind, key, role, source, confidence):
#   ann    -> annotation simple-name (lowercased) present on the class
#   suffix -> class name ends with key
#   pkg    -> key is a substring of the class's package
# Edit this table to retune role classification — precedence is list order
# (annotation > name suffix > package), the explicit mapping the design asked for.
_CLASS_ROLE_RULES: list[tuple[str, str, str, str, str]] = [
    ("ann", "restcontroller", "controller", "annotation", "HIGH"),
    ("ann", "controller", "controller", "annotation", "HIGH"),
    ("ann", "service", "service", "annotation", "HIGH"),
    ("ann", "repository", "repository", "annotation", "HIGH"),
    ("ann", "configuration", "config", "annotation", "HIGH"),
    ("ann", "configurationproperties", "config", "annotation", "HIGH"),
    ("ann", "entity", "entity", "annotation", "HIGH"),
    ("suffix", "controller", "controller", "name_suffix", "MEDIUM"),
    ("suffix", "service", "service", "name_suffix", "MEDIUM"),
    ("suffix", "repository", "repository", "name_suffix", "MEDIUM"),
    ("suffix", "dao", "repository", "name_suffix", "MEDIUM"),
    ("suffix", "config", "config", "name_or_package", "MEDIUM"),
    ("pkg", ".config", "config", "name_or_package", "MEDIUM"),
    ("suffix", "util", "util", "name_suffix", "LOW"),
    ("suffix", "utils", "util", "name_suffix", "LOW"),
    ("pkg", ".controller", "controller", "package", "LOW"),
    ("pkg", ".service", "service", "package", "LOW"),
    ("pkg", ".repo", "repository", "package", "LOW"),
    ("pkg", ".repository", "repository", "package", "LOW"),
]


def _classify_roles(nodes: list[Node], edges: list[Edge]) -> Counter:
    """Deterministic component-role tagging from annotations/name/package signals.

    Returns a diagnostics counter keyed by (role, source) so role-assignment
    quality is observable (e.g. how many roles came from HIGH-confidence
    annotations vs LOW-confidence package fallbacks)."""
    by_id = {n.id: n for n in nodes}
    parent_of: dict[str, str] = {}
    for e in edges:
        if e.type == "CONTAINS":
            parent_of[e.dst] = e.src

    ann_by_src: dict[str, set[str]] = defaultdict(set)
    exposes_src: set[str] = set()
    for e in edges:
        if e.type == "ANNOTATED_WITH":
            dst = by_id.get(e.dst)
            if dst and dst.label == "Annotation":
                ann_by_src[e.src].add(dst.name.lower())
        elif e.type == "EXPOSES":
            exposes_src.add(e.src)

    diag: Counter = Counter()

    def assign(n: Node, role: str, source: str, confidence: str) -> None:
        n.component_role = role
        n.role_source = source
        n.role_confidence = confidence
        diag[(role, source)] += 1

    for n in nodes:
        if n.label != "Class":
            continue
        anns = ann_by_src.get(n.id, set())
        name = (n.name or "").lower()
        pkg = (n.package or "").lower()
        for kind, key, role, source, conf in _CLASS_ROLE_RULES:
            if (
                (kind == "ann" and key in anns)
                or (kind == "suffix" and name.endswith(key))
                or (kind == "pkg" and key in pkg)
            ):
                assign(n, role, source, conf)
                break

    for n in nodes:
        if n.label != "Function":
            continue
        if n.id in exposes_src:
            assign(n, "endpoint_handler", "edge_exposes", "HIGH")
            continue
        cur = parent_of.get(n.id)
        matched = False
        while cur is not None:
            p = by_id.get(cur)
            if p is None:
                break
            if p.label == "Class" and p.component_role:
                # A method on a Controller class is effectively an endpoint
                # even without an explicit route decorator we recognize
                # (inherited routing, class-based views, framework patterns
                # our extractor doesn't parse yet) — fall back to
                # endpoint_handler at lower confidence instead of leaving it
                # tagged merely "controller" (which never seeds taint
                # analysis, since that only starts from endpoint_handler
                # roots).
                if p.component_role == "controller":
                    assign(n, "endpoint_handler", "owner_class_controller", "MEDIUM")
                else:
                    assign(n, p.component_role, "owner_class", "MEDIUM")
                matched = True
                break
            cur = parent_of.get(cur)
        if matched:
            continue
        # Module-level free function (no owning class, or owning class has no
        # role either) — tag it "helper" at LOW confidence instead of leaving
        # component_role blank. An unlabeled function otherwise reads to
        # downstream consumers (Stage 1/2 architecture prompts) as an
        # unexplained anomaly in an otherwise clean role sequence, which was
        # observed to cause spurious layering_violation/missing_authorization
        # findings on ordinary helpers like a `normalize_term`-style sanitizer
        # that simply isn't a class method.
        assign(n, "helper", "no_owning_class", "LOW")

    return diag


def _derive_module_ownership_and_uses(
    nodes: list[Node],
    edges: list[Edge],
    repo: str,
    module_root_depth: int = 1,
    module_roots: tuple[str, ...] = (),
) -> tuple[list[Node], list[Edge], list[Edge]]:
    """Derive Module ownership and aggregate low-level deps into USES.

    Output edges are additive and deterministic. Module boundaries are
    configurable rather than hard-wired to the first package segment:

      - ``module_roots``: explicit module-root prefixes (longest match wins),
        e.g. ``("com.acme.billing", "com.acme.orders")`` to model real modules
        instead of guessing. Matched against the node's package or file path.
      - ``module_root_depth``: when no explicit root matches, how many leading
        package/path segments form the module key (default 1 = first segment,
        the original behaviour).

    Component-level USES carry a ``cross_module`` / ``intra_module`` strategy tag
    so boundary-crossing dependencies (the blast-radius signal) are queryable.
    """
    by_id = {n.id: n for n in nodes}
    parent_of: dict[str, str] = {}
    for e in edges:
        if e.type == "CONTAINS":
            parent_of[e.dst] = e.src

    module_nodes: list[Node] = []
    ownership_edges: list[Edge] = []
    uses_edges: list[Edge] = []

    seen_mod_nodes: set[str] = set()
    seen_mod_edges: set[tuple[str, str]] = set()
    existing_edge_keys: set[tuple[str, str, str]] = {(e.type, e.src, e.dst) for e in edges}

    # Longest-prefix-first so a more specific root wins over a broader one.
    roots = sorted(module_roots, key=len, reverse=True)

    def module_key_for(n: Node) -> str:
        if n.package:
            segs = [s for s in n.package.split(".") if s]
        elif n.file:
            f = n.file.replace("\\", "/")
            segs = [
                p.replace(".py", "").replace(".java", "")
                for p in f.split("/") if p
            ]
        else:
            return ""
        if not segs:
            return ""
        dotted = ".".join(segs)
        for root in roots:
            if dotted == root or dotted.startswith(root + "."):
                return root
        depth = max(1, module_root_depth)
        return ".".join(segs[:depth])

    allowed_owned_labels = {
        "File", "Class", "Function", "Field", "Endpoint", "Event", "Policy", "Annotation",
    }
    for n in nodes:
        if n.label not in allowed_owned_labels:
            continue
        mkey = module_key_for(n)
        if not mkey:
            continue
        mid = make_id(repo, f"module:{mkey}", "module")
        if mid not in seen_mod_nodes and mid not in by_id:
            seen_mod_nodes.add(mid)
            module_nodes.append(Node(
                id=mid, label="Module", name=mkey, fqn=f"module:{mkey}",
                repo=repo, kind="module", package=mkey,
                extractor="derivation", confidence=Confidence.INFERRED.value,
            ))
        n.module_id = mid
        k = (n.id, mid)
        if k in seen_mod_edges or ("BELONGS_TO", n.id, mid) in existing_edge_keys:
            continue
        seen_mod_edges.add(k)
        ownership_edges.append(Edge(
            "BELONGS_TO", n.id, mid,
            confidence=Confidence.INFERRED.value,
            origin=Origin.DERIVED.value,
            extractor="derivation",
            strategy="module_from_package_or_path",
            evidence_file=n.file,
            evidence_line=n.start_line,
        ))

    # Re-index after appending derived modules.
    by_id = {n.id: n for n in [*nodes, *module_nodes]}

    def owner_component(node_id: str) -> str | None:
        cur = node_id
        while cur is not None:
            n = by_id.get(cur)
            if n is None:
                return None
            if n.label in ("Class", "File", "Endpoint", "Module"):
                return cur
            cur = parent_of.get(cur)
        return None

    base_dep_types = {
        "CALLS", "PASSES", "READS", "WRITES", "IMPORTS", "INSTANTIATES",
        "CALLS_API", "REFERENCES", "EMITS_EVENT", "CONSUMES_EVENT",
        "REQUIRES_AUTH", "ENFORCES_POLICY", "EXTENDS", "IMPLEMENTS", "OVERRIDES",
    }

    comp_uses_support: dict[tuple[str, str], Edge] = {}
    for e in edges:
        if e.type not in base_dep_types:
            continue
        s = owner_component(e.src)
        d = owner_component(e.dst)
        if not s or not d or s == d:
            continue
        k = (s, d)
        if k not in comp_uses_support:
            comp_uses_support[k] = e

    def module_of(node_id: str) -> str:
        n = by_id.get(node_id)
        if n is None:
            return ""
        return n.id if n.label == "Module" else n.module_id

    for (s, d), support in comp_uses_support.items():
        if ("USES", s, d) in existing_edge_keys:
            continue
        sm, dm = module_of(s), module_of(d)
        boundary = "cross_module" if (sm and dm and sm != dm) else "intra_module"
        uses_edges.append(Edge(
            "USES", s, d,
            confidence=Confidence.INFERRED.value,
            origin=Origin.DERIVED.value,
            extractor="derivation",
            strategy=f"component_aggregate:{boundary}",
            evidence_file=support.evidence_file,
            evidence_line=support.evidence_line,
            evidence_col=support.evidence_col,
        ))
        existing_edge_keys.add(("USES", s, d))

    # Module-level USES from component USES ownership.
    module_uses_pairs: set[tuple[str, str]] = set()
    for e in uses_edges:
        src_n = by_id.get(e.src)
        dst_n = by_id.get(e.dst)
        if src_n is None or dst_n is None:
            continue
        sm = src_n.module_id
        dm = dst_n.module_id
        if not sm or not dm or sm == dm:
            continue
        module_uses_pairs.add((sm, dm))

    for sm, dm in module_uses_pairs:
        if ("USES", sm, dm) in existing_edge_keys:
            continue
        uses_edges.append(Edge(
            "USES", sm, dm,
            confidence=Confidence.INFERRED.value,
            origin=Origin.DERIVED.value,
            extractor="derivation",
            strategy="module_aggregate",
        ))
        existing_edge_keys.add(("USES", sm, dm))

    return module_nodes, ownership_edges, uses_edges
