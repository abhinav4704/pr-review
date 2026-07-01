"""Regression harness for the semantic/architecture edges (no Neo4j required).

Runs the DB-free portion of the index pipeline over `fixtures/` and asserts that
the edges added in the events/auth/architecture pass are actually produced:

    DEFINES, PASSES, EMITS_EVENT, CONSUMES_EVENT, REQUIRES_AUTH, ENFORCES_POLICY,
    BELONGS_TO, USES (component- and module-level), plus the Event/Policy/Module
    nodes they create. REFERENCES is the resolver-only fallback (extraction never
    emits a dotted call target) so it is checked directly against the resolver.

Mirrors `graph_rag.pipeline.index_repo` step-for-step with `scip=False`, so the
assertions track the real pipeline, not a reimplementation.

    ./.venv/bin/python validate_fixtures.py [fixtures_dir]

Exit code 0 = all checks passed, 1 = a regression was detected.
"""
from __future__ import annotations

import os
import sys
from collections import Counter

from graph_rag import extractors
from graph_rag.canonical_ir import from_extractor, merge_bundles
from graph_rag.discovery import discover
from graph_rag.models import Node, RawRef
from graph_rag.pipeline import (
    _attach_call_metrics,
    _build_package_tree,
    _classify_roles,
    _derive_module_ownership_and_uses,
    _derive_overrides,
)
from graph_rag.resolver import resolve
from graph_rag.validator import validate_graph

REPO = "fixtures"


def build_graph(root: str):
    """Reproduce index_repo's in-memory stages (heuristic only, no SCIP, no DB)."""
    files = discover(root)
    bundles = []
    for f in files:
        nodes, edges, refs = extractors.extract(f, REPO)
        bundles.append(from_extractor(nodes, edges, refs))
    canonical = merge_bundles(bundles)
    nodes, edges, refs = canonical.nodes, canonical.edges, canonical.refs

    extra_nodes, resolved_edges, _coverage = resolve(nodes, edges, refs, REPO)
    nodes = [*nodes, *extra_nodes]
    edges = [*edges, *resolved_edges]

    edges += _derive_overrides(nodes, edges)
    pkg_nodes, pkg_edges = _build_package_tree(nodes, REPO)
    nodes += pkg_nodes
    edges += pkg_edges

    role_diag = _classify_roles(nodes, edges)
    mod_nodes, mod_edges, uses_edges = _derive_module_ownership_and_uses(nodes, edges, REPO)
    nodes += mod_nodes
    edges += mod_edges + uses_edges

    _attach_call_metrics(nodes, edges)
    validation = validate_graph(nodes, edges)
    return files, nodes, edges, validation, role_diag


# --- checks ----------------------------------------------------------------

class Checks:
    def __init__(self) -> None:
        self.results: list[tuple[bool, str]] = []

    def expect(self, ok: bool, msg: str) -> None:
        self.results.append((bool(ok), msg))

    def at_least(self, count: int, minimum: int, label: str) -> None:
        self.expect(count >= minimum, f"{label}: got {count}, expected >= {minimum}")

    def report(self) -> int:
        failed = [m for ok, m in self.results if not ok]
        for ok, m in self.results:
            print(f"  {'PASS' if ok else 'FAIL'}  {m}")
        print(f"\n{len(self.results) - len(failed)}/{len(self.results)} checks passed")
        return 1 if failed else 0


def edge_present(edges, etype, src_pred=None, dst_pred=None, by_id=None) -> int:
    n = 0
    for e in edges:
        if e.type != etype:
            continue
        if src_pred and not src_pred(by_id.get(e.src)):
            continue
        if dst_pred and not dst_pred(by_id.get(e.dst)):
            continue
        n += 1
    return n


def references_resolver_check(chk: Checks) -> None:
    """Direct resolver check for the dotted-tail REFERENCES fallback: an
    unresolved *dotted* call target whose tail matches an in-repo function."""
    caller = Node(id="n_caller", label="Function", name="caller", fqn="m.caller", repo=REPO, kind="function")
    target = Node(id="n_compute", label="Function", name="compute", fqn="m.compute", repo=REPO, kind="function")
    ref = RawRef("CALLS", "n_caller", "utils.compute", "call", ref_file="m.py", ref_line=1)
    _extra, out_edges, _cov = resolve([caller, target], [], [ref], REPO)
    refs = [e for e in out_edges if e.type == "REFERENCES" and e.dst == "n_compute"]
    chk.expect(
        len(refs) == 1 and refs[0].confidence == "INFERRED",
        f"REFERENCES (resolver) dotted 'utils.compute' -> compute (got {len(refs)})",
    )


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "fixtures")
    files, nodes, edges, validation, role_diag = build_graph(root)
    by_id = {n.id: n for n in nodes}

    edge_counts = Counter(e.type for e in edges)
    node_counts = Counter(n.label for n in nodes)

    print(f"fixtures: {len(files)} files, {len(nodes)} nodes, {len(edges)} edges\n")
    print("node labels: " + ", ".join(f"{k}={v}" for k, v in sorted(node_counts.items())))
    print("edge types:  " + ", ".join(f"{k}={v}" for k, v in sorted(edge_counts.items())))
    print()

    chk = Checks()

    # Newly-added semantic edges must each fire from the fixtures.
    chk.at_least(edge_counts["DEFINES"], 6, "DEFINES")
    chk.at_least(edge_counts["PASSES"], 2, "PASSES (python + java)")
    chk.at_least(edge_counts["EMITS_EVENT"], 2, "EMITS_EVENT (python + java)")
    chk.at_least(edge_counts["CONSUMES_EVENT"], 3, "CONSUMES_EVENT (py decorator + py call + java)")
    chk.at_least(edge_counts["REQUIRES_AUTH"], 1, "REQUIRES_AUTH (python @requires_auth)")
    chk.at_least(edge_counts["ENFORCES_POLICY"], 2, "ENFORCES_POLICY (python + java)")

    # Materialized semantic nodes.
    chk.at_least(node_counts["Event"], 2, "Event nodes (OrderPlaced, OrderShipped)")
    chk.at_least(node_counts["Policy"], 1, "Policy nodes")
    chk.at_least(node_counts["Module"], 2, "Module nodes (orders, billing, com)")

    # REFERENCES now reachable from extraction: a call on an unknown receiver
    # (tracer.stamp) is demoted from CALLS to a weak REFERENCES symbol-use edge.
    stamp_refs = edge_present(
        edges, "REFERENCES",
        dst_pred=lambda n: n is not None and n.name == "stamp", by_id=by_id,
    )
    chk.at_least(stamp_refs, 1, "REFERENCES (extraction): tracer.stamp demoted to REFERENCES")

    # Architecture derivation.
    chk.at_least(edge_counts["BELONGS_TO"], 6, "BELONGS_TO (every owned node -> Module)")
    is_module = lambda n: n is not None and n.label == "Module"
    module_uses = edge_present(edges, "USES", src_pred=is_module, dst_pred=is_module, by_id=by_id)
    chk.at_least(edge_counts["USES"], 1, "USES (any level)")
    chk.at_least(module_uses, 1, "USES module-level (orders -> billing)")
    cross_uses = sum(1 for e in edges if e.type == "USES" and "cross_module" in e.strategy)
    chk.at_least(cross_uses, 1, "USES component-level tagged cross_module")

    # Roles: the Java @RestController class should be tagged + diagnostics emitted.
    controllers = [n for n in nodes if n.component_role == "controller"]
    chk.at_least(len(controllers), 1, "role classification: controller tagged")
    chk.expect(bool(role_diag), f"role diagnostics emitted ({dict(role_diag)})")
    chk.expect(
        any(src == "annotation" for (_role, src) in role_diag),
        "role diagnostics include a HIGH-confidence annotation assignment",
    )

    # REFERENCES dotted-tail fallback (resolver-level).
    references_resolver_check(chk)

    # Graph must stay structurally valid.
    errors = validation.get("errors", []) if isinstance(validation, dict) else []
    chk.expect(not errors, f"graph validation: {len(errors)} error(s)" + (f" -> {errors[:3]}" if errors else ""))

    print("checks:")
    return chk.report()


if __name__ == "__main__":
    raise SystemExit(main())
