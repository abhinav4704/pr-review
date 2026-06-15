"""Tests for impact-chain tracing (pr_review.impact).

These build synthetic CodeGraphs directly (no tree-sitter / no LLM) so the
graph-traversal logic can be asserted deterministically.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pr_review.graph import CodeGraph
from pr_review.diff import FileDiff
from pr_review import impact


# ── builders ────────────────────────────────────────────────────────────────────
def add_fn(cg, path, name, start, end, qualname=None, kind="function", is_test=False):
    qn = qualname or name
    nid = f"{path}::{qn}"
    cg.g.add_node(nid, kind=kind, path=path, name=name, qualname=qn,
                  start_line=start, end_line=end, lang="python", is_test=is_test)
    return nid


def dep(cg, dependent, dependency, etype="calls", confidence="unique"):
    """dependent depends on dependency (e.g. dependent CALLS dependency)."""
    cg.g.add_edge(dependent, dependency, type=etype, confidence=confidence)


def fd(path, added):
    return FileDiff(path=path, is_new=False, is_deleted=False,
                    added_lines=set(added), changed_lines=set())


# ── canonical: f1 (schema) <- f2 (returns it) <- g1 (unchanged consumer) ─────────
def test_single_chain_through_changed_intermediate():
    cg = CodeGraph(root=".")
    f1 = add_fn(cg, "a.py", "f1", 1, 5)       # schema
    f2 = add_fn(cg, "a.py", "f2", 7, 12)      # returns the schema
    g1 = add_fn(cg, "b.py", "g1", 1, 6)       # consumes f2, NOT changed
    dep(cg, f2, f1)        # f2 calls f1
    dep(cg, g1, f2)        # g1 calls f2

    res = impact.analyze_impact(cg, [fd("a.py", [2, 8])])

    assert len(res.clusters) == 1
    cluster = res.clusters[0]
    assert set(cluster.members) == {f1, f2}          # f1 & f2 are ONE unit
    assert len(cluster.chains) == 1                   # exactly one consumer
    chain = cluster.chains[0]
    assert chain.consumer.qualname == "g1"
    assert chain.consumer.changed is False
    assert chain.source.changed is True


def test_overlapping_callers_not_duplicated():
    """A consumer that calls BOTH changed functions appears once, not twice."""
    cg = CodeGraph(root=".")
    f1 = add_fn(cg, "a.py", "f1", 1, 5)
    f2 = add_fn(cg, "a.py", "f2", 7, 12)
    g1 = add_fn(cg, "b.py", "g1", 1, 6)
    dep(cg, f2, f1)
    dep(cg, g1, f1)        # g1 calls f1
    dep(cg, g1, f2)        # g1 also calls f2  -> two routes to g1

    res = impact.analyze_impact(cg, [fd("a.py", [2, 8])])
    consumers = [c.consumer.qualname for c in res.clusters[0].chains]
    assert consumers == ["g1"]                       # exactly one, no repeats


def test_cycle_terminates():
    """Mutual recursion among changed nodes must not loop forever."""
    cg = CodeGraph(root=".")
    f1 = add_fn(cg, "a.py", "f1", 1, 5)
    m = add_fn(cg, "a.py", "m", 7, 12)
    c = add_fn(cg, "b.py", "c", 1, 6)
    dep(cg, f1, m)         # f1 calls m
    dep(cg, m, f1)         # m calls f1   -> cycle
    dep(cg, c, f1)         # external consumer

    res = impact.analyze_impact(cg, [fd("a.py", [2, 8])])
    cluster = res.clusters[0]
    assert set(cluster.members) == {f1, m}
    assert [ch.consumer.qualname for ch in cluster.chains] == ["c"]


def test_hub_consumers_capped():
    cg = CodeGraph(root=".")
    f = add_fn(cg, "a.py", "hub", 1, 5)
    for i in range(30):
        ci = add_fn(cg, f"c{i}.py", f"c{i}", 1, 4)
        dep(cg, ci, f)

    res = impact.analyze_impact(cg, [fd("a.py", [2])], max_consumers_per_cluster=15)
    cluster = res.clusters[0]
    assert len(cluster.chains) == 15
    assert cluster.extra_consumers == 15


def test_uncertain_edges_included_but_flagged_and_downranked():
    cg = CodeGraph(root=".")
    f1 = add_fn(cg, "a.py", "f1", 1, 5)
    ga = add_fn(cg, "b.py", "ga", 1, 6)
    gb = add_fn(cg, "c.py", "gb", 1, 6)
    dep(cg, ga, f1, confidence="ambiguous")   # guessed link
    dep(cg, gb, f1, confidence="unique")      # sure link

    # default: both shown, ambiguous one flagged uncertain and ranked lower
    res = impact.analyze_impact(cg, [fd("a.py", [2])])
    chains = {c.consumer.qualname: c for c in res.clusters[0].chains}
    assert set(chains) == {"ga", "gb"}
    assert chains["ga"].uncertain is True
    assert chains["gb"].uncertain is False
    assert res.clusters[0].chains[0].consumer.qualname == "gb"   # sure link first

    # opt-in strict mode drops uncertain links entirely
    res2 = impact.analyze_impact(cg, [fd("a.py", [2])], exclude_ambiguous=True)
    assert {c.consumer.qualname for c in res2.clusters[0].chains} == {"gb"}


def test_method_call_resolved_via_instantiation():
    """`u = User(); u.save()` resolves to User.save even when Order.save exists."""
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))
    import tempfile, textwrap
    from pr_review.graph import build_graph

    d = tempfile.mkdtemp()
    open(_os.path.join(d, "models.py"), "w").write(textwrap.dedent('''
        class User:
            def save(self):
                return 1
        class Order:
            def save(self):
                return 2
    '''))
    open(_os.path.join(d, "svc.py"), "w").write(textwrap.dedent('''
        from models import User
        def register(u):
            user = User()
            return user.save()
    '''))
    cg = build_graph(d)
    edge = cg.g.get_edge_data("svc.py::register", "models.py::User.save")
    assert edge is not None and edge["type"] == "calls"
    assert edge["confidence"] == "unique"        # disambiguated by instantiation
    # the wrong candidate must NOT be linked
    assert cg.g.get_edge_data("svc.py::register", "models.py::Order.save") is None


def test_modified_consumer_downranked():
    """A consumer the PR already touched scores below a clean consumer."""
    src = impact.ChainNode(
        node_id="a.py::f", role="source", changed=True, change_type="signature",
        modified_in_pr=False, kind="function", qualname="f", path="a.py",
        start_line=1, end_line=5, is_test=False)
    clean = impact.ChainNode(
        node_id="b.py::g", role="consumer", changed=False, change_type=None,
        modified_in_pr=False, kind="function", qualname="g", path="b.py",
        start_line=1, end_line=6, is_test=False)
    touched = impact.ChainNode(
        node_id="b.py::g", role="consumer", changed=False, change_type=None,
        modified_in_pr=True, kind="function", qualname="g", path="b.py",
        start_line=1, end_line=6, is_test=False)

    chain_clean = impact.Chain(nodes=[src, clean], consumer_id="b.py::g", distance=1)
    chain_touched = impact.Chain(nodes=[src, touched], consumer_id="b.py::g",
                                 distance=1, modified_consumer=True)
    assert impact._score_chain(chain_touched) < impact._score_chain(chain_clean)


def test_field_delta_and_gone_fields():
    diff = ('@@\n'
            '-    return {"name": name, "age": age}\n'
            '+    return {"name": name, "years": age}\n')
    removed, added = impact.field_delta(diff)
    assert "age" in removed and "name" in removed
    assert "years" in added and "name" in added
    assert impact.gone_fields({"schema.py": diff}) == {"age"}


def test_field_hits_flag_and_boost_consumer():
    cg = CodeGraph(root=".")
    f1 = add_fn(cg, "a.py", "make_user", 1, 3)
    user = add_fn(cg, "b.py", "uses_age", 1, 6)       # references ["age"]
    other = add_fn(cg, "c.py", "no_field", 1, 6)      # references nothing
    dep(cg, user, f1)
    dep(cg, other, f1)

    def src_of(node):
        return {'uses_age': 'p = make_user()\nreturn p["age"]',
                'no_field': 'return make_user()'}.get(node.qualname, "")

    res = impact.analyze_impact(cg, [fd("a.py", [2])],
                                gone={"age"}, consumer_src_fn=src_of)
    chains = {c.consumer.qualname: c for c in res.clusters[0].chains}
    assert chains["uses_age"].field_hits == ["age"]
    assert chains["no_field"].field_hits == []
    # the consumer that still uses the removed field ranks first
    assert res.clusters[0].chains[0].consumer.qualname == "uses_age"


def test_entrypoint_consumer_ranks_high():
    cg = CodeGraph(root=".")
    f = add_fn(cg, "a.py", "f", 1, 5)
    route = add_fn(cg, "api.py", "handler", 1, 8, kind="route")
    plain = add_fn(cg, "util.py", "helper", 1, 8)
    dep(cg, route, f)
    dep(cg, plain, f)

    res = impact.analyze_impact(cg, [fd("a.py", [2])])
    chains = res.clusters[0].chains
    # the route consumer should be ranked first
    assert chains[0].consumer.kind == "route"
    assert chains[0].is_entrypoint is True
