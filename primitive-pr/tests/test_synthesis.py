"""Tests for pr_review/synthesis.py — Track C Issue Synthesizer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List
from unittest.mock import MagicMock

import pytest

from pr_review.findings import Finding
from pr_review.impact import Chain, ChainNode, Cluster
from pr_review.pr_passes import ClusterReview
from pr_review.synthesis import (
    IssueReport,
    _build_synthesis_dossier,
    _findings_for_cluster,
    _parse_issue_reports,
    finding_id,
    synthesize_all,
    synthesize_cluster,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_finding(title="SQL injection", severity="high", file="auth.py", line=10,
                  category="vulnerability") -> Finding:
    return Finding(
        category=category, severity=severity, file=file, line=line,
        title=title, explanation="explains", evidence="code", recommendation="fix",
    )


def _make_chain_node(nid="n1", qualname="auth.validate", path="auth.py",
                     start=5, end=20, changed=True, role="source") -> ChainNode:
    return ChainNode(
        node_id=nid, role=role, changed=changed, change_type="signature",
        modified_in_pr=changed, kind="function", qualname=qualname,
        path=path, start_line=start, end_line=end, is_test=False,
    )


def _make_cluster_review(members=("n1",), findings=None, chains=None) -> ClusterReview:
    cluster = Cluster(
        members=list(members),
        chains=chains or [],
    )
    return ClusterReview(cluster=cluster, findings=findings or [])


def _make_cg(nodes: dict) -> MagicMock:
    """Fake CodeGraph whose .has() and .node() are backed by a dict."""
    cg = MagicMock()
    cg.has.side_effect = lambda nid: nid in nodes
    cg.node.side_effect = lambda nid: nodes.get(nid, {})
    return cg


def _llm_returning(payload) -> callable:
    """Returns a complete_fn that always returns the given JSON payload."""
    def complete(system, user):
        return json.dumps(payload)
    return complete


# ── _parse_issue_reports ──────────────────────────────────────────────────────

def test_parse_valid_issue_report():
    raw = json.dumps([{
        "title": "Auth bypass via forged token",
        "severity": "critical",
        "root_cause_file": "auth.py",
        "root_cause_line": 42,
        "root_cause_summary": "Token validation removed.",
        "exploit_path": "1. Send forged token\n2. Bypass auth",
        "impact_chain": "auth.py → middleware.py → billing.py",
        "affected_files": ["middleware.py", "billing.py"],
        "recommendation": "Restore token validation.",
        "confidence": "high",
    }])
    reports = _parse_issue_reports(raw, cluster_idx=0, source_findings=[])
    assert len(reports) == 1
    r = reports[0]
    assert r.title == "Auth bypass via forged token"
    assert r.severity == "critical"
    assert r.root_cause_line == 42
    assert "middleware.py" in r.affected_files
    assert r.confidence == "high"


def test_parse_strips_markdown_fences():
    payload = [{"title": "XSS", "severity": "high", "root_cause_file": "views.py",
                "root_cause_line": 5, "root_cause_summary": "s", "exploit_path": "e",
                "impact_chain": "a → b", "affected_files": [], "recommendation": "r",
                "confidence": "medium"}]
    raw = "```json\n" + json.dumps(payload) + "\n```"
    reports = _parse_issue_reports(raw, cluster_idx=0, source_findings=[])
    assert len(reports) == 1
    assert reports[0].title == "XSS"


def test_parse_empty_returns_empty():
    assert _parse_issue_reports("[]", 0, []) == []
    assert _parse_issue_reports("", 0, []) == []
    assert _parse_issue_reports("not json at all", 0, []) == []


def test_parse_unknown_severity_defaults_medium():
    raw = json.dumps([{"title": "T", "severity": "extreme",
                       "root_cause_file": "f.py", "root_cause_line": 1,
                       "root_cause_summary": "s", "exploit_path": "e",
                       "impact_chain": "", "affected_files": [],
                       "recommendation": "r", "confidence": "high"}])
    reports = _parse_issue_reports(raw, 0, [])
    assert reports[0].severity == "medium"


def test_parse_affected_files_as_comma_string():
    """LLM sometimes returns affected_files as a comma-separated string."""
    raw = json.dumps([{"title": "T", "severity": "high",
                       "root_cause_file": "a.py", "root_cause_line": 1,
                       "root_cause_summary": "s", "exploit_path": "e",
                       "impact_chain": "", "affected_files": "b.py, c.py",
                       "recommendation": "r", "confidence": "low"}])
    reports = _parse_issue_reports(raw, 0, [])
    assert reports[0].affected_files == ["b.py", "c.py"]


# ── _findings_for_cluster ─────────────────────────────────────────────────────

def test_findings_for_cluster_line_in_range():
    """Finding whose line falls inside a member's start–end range is matched."""
    cg = _make_cg({"n1": {"path": "auth.py", "start_line": 1, "end_line": 50}})
    cluster = Cluster(members=["n1"])
    f = _make_finding(file="auth.py", line=25)
    result = _findings_for_cluster(cg, cluster, {"auth.py": [f]})
    assert f in result


def test_findings_for_cluster_line_outside_range_not_matched():
    cg = _make_cg({"n1": {"path": "auth.py", "start_line": 1, "end_line": 50}})
    cluster = Cluster(members=["n1"])
    f = _make_finding(file="auth.py", line=99)
    result = _findings_for_cluster(cg, cluster, {"auth.py": [f]})
    assert f not in result


def test_findings_for_cluster_line_zero_matches_file():
    """line==0 → file-only match."""
    cg = _make_cg({"n1": {"path": "auth.py", "start_line": 1, "end_line": 50}})
    cluster = Cluster(members=["n1"])
    f = _make_finding(file="auth.py", line=0)
    result = _findings_for_cluster(cg, cluster, {"auth.py": [f]})
    assert f in result


def test_findings_for_cluster_different_file_not_matched():
    cg = _make_cg({"n1": {"path": "auth.py", "start_line": 1, "end_line": 50}})
    cluster = Cluster(members=["n1"])
    f = _make_finding(file="other.py", line=10)
    result = _findings_for_cluster(cg, cluster, {"other.py": [f]})
    assert f not in result


def test_findings_for_cluster_no_duplicates_across_members():
    """Same finding matched by two members should appear only once."""
    cg = _make_cg({
        "n1": {"path": "auth.py", "start_line": 1, "end_line": 30},
        "n2": {"path": "auth.py", "start_line": 20, "end_line": 50},
    })
    cluster = Cluster(members=["n1", "n2"])
    f = _make_finding(file="auth.py", line=25)  # inside both ranges
    result = _findings_for_cluster(cg, cluster, {"auth.py": [f]})
    assert result.count(f) == 1


# ── synthesize_cluster ────────────────────────────────────────────────────────

def test_synthesize_cluster_returns_issue_reports():
    cg = _make_cg({"n1": {"path": "auth.py", "start_line": 1, "end_line": 50,
                           "kind": "function", "qualname": "validate_token",
                           "name": "validate_token"}})
    cluster_review = _make_cluster_review(
        members=("n1",),
        findings=[_make_finding(title="Token check removed", severity="critical",
                                file="auth.py", line=10)],
    )
    track_a = {"auth.py": [_make_finding(title="SQL injection", severity="high",
                                          file="auth.py", line=15)]}

    llm_payload = [{
        "title": "Auth bypass and SQL injection",
        "severity": "critical",
        "root_cause_file": "auth.py",
        "root_cause_line": 10,
        "root_cause_summary": "Token validation removed, SQL injection in same function.",
        "exploit_path": "1. Send forged token\n2. Execute SQL",
        "impact_chain": "auth.py → api.py",
        "affected_files": ["api.py"],
        "recommendation": "Restore validation and use parameterised queries.",
        "confidence": "high",
    }]
    reports = synthesize_cluster(cg, cluster_review, track_a,
                                 _llm_returning(llm_payload), cluster_idx=0)
    assert len(reports) == 1
    r = reports[0]
    assert r.severity == "critical"
    assert r.cluster_idx == 0
    # source_findings must include both Track A and Track B findings
    titles = {f.title for f in r.source_findings}
    assert "SQL injection" in titles
    assert "Token check removed" in titles


def test_synthesize_cluster_no_signal_returns_empty():
    """Cluster with no Track A findings, no Track B findings, no chains → skip LLM."""
    cg = _make_cg({"n1": {"path": "auth.py", "start_line": 1, "end_line": 50,
                           "kind": "function", "qualname": "foo", "name": "foo"}})
    cluster_review = _make_cluster_review(members=("n1",), findings=[], chains=[])
    called = []

    def complete(system, user):
        called.append(1)
        return "[]"

    reports = synthesize_cluster(cg, cluster_review, {}, complete, cluster_idx=0)
    assert reports == []
    assert not called, "LLM should not be called when there is no signal"


def test_synthesize_cluster_llm_returns_empty_produces_no_reports():
    cg = _make_cg({"n1": {"path": "auth.py", "start_line": 1, "end_line": 50,
                           "kind": "function", "qualname": "validate_token",
                           "name": "validate_token"}})
    cr = _make_cluster_review(
        members=("n1",),
        findings=[_make_finding()],
    )
    reports = synthesize_cluster(cg, cr, {}, _llm_returning([]), cluster_idx=0)
    assert reports == []


# ── synthesize_all ────────────────────────────────────────────────────────────

def test_synthesize_all_concurrent_returns_sorted_by_severity():
    cg = _make_cg({
        "n1": {"path": "a.py", "start_line": 1, "end_line": 20,
               "kind": "function", "qualname": "f1", "name": "f1"},
        "n2": {"path": "b.py", "start_line": 1, "end_line": 20,
               "kind": "function", "qualname": "f2", "name": "f2"},
    })
    cr1 = _make_cluster_review(members=("n1",),
                                findings=[_make_finding(severity="medium", file="a.py", line=5)])
    cr2 = _make_cluster_review(members=("n2",),
                                findings=[_make_finding(severity="critical", file="b.py", line=5,
                                                        title="RCE found")])

    def make_llm(severity, title):
        payload = [{
            "title": title, "severity": severity,
            "root_cause_file": "x.py", "root_cause_line": 1,
            "root_cause_summary": "s", "exploit_path": "e",
            "impact_chain": "", "affected_files": [],
            "recommendation": "r", "confidence": "high",
        }]
        return json.dumps(payload)

    responses = {
        id(cr1): make_llm("medium", "Medium issue"),
        id(cr2): make_llm("critical", "Critical RCE"),
    }

    call_map = {id(cr1): cr1, id(cr2): cr2}

    def complete(system, user):
        # determine which cluster by content hint
        if "f1" in user:
            return responses[id(cr1)]
        return responses[id(cr2)]

    reports = synthesize_all(cg, [cr1, cr2], {}, complete, max_workers=2)
    assert len(reports) == 2
    assert reports[0].severity == "critical"   # sorted highest first
    assert reports[1].severity == "medium"


def test_synthesize_all_deduplicates_same_location():
    """Two clusters producing the same root_cause_file:line → deduplicated."""
    cg = _make_cg({
        "n1": {"path": "auth.py", "start_line": 1, "end_line": 50,
               "kind": "function", "qualname": "f1", "name": "f1"},
        "n2": {"path": "auth.py", "start_line": 10, "end_line": 40,
               "kind": "function", "qualname": "f2", "name": "f2"},
    })
    cr1 = _make_cluster_review(members=("n1",),
                                findings=[_make_finding(file="auth.py", line=12)])
    cr2 = _make_cluster_review(members=("n2",),
                                findings=[_make_finding(file="auth.py", line=12)])

    same_payload = json.dumps([{
        "title": "Auth bypass", "severity": "critical",
        "root_cause_file": "auth.py", "root_cause_line": 12,
        "root_cause_summary": "s", "exploit_path": "e",
        "impact_chain": "", "affected_files": [],
        "recommendation": "r", "confidence": "high",
    }])

    def complete(system, user):
        return same_payload

    reports = synthesize_all(cg, [cr1, cr2], {}, complete, max_workers=2)
    # Same (file, line//5, title) → deduplicated to 1
    assert len(reports) == 1


def test_synthesize_all_empty_clusters_returns_empty():
    cg = _make_cg({})
    reports = synthesize_all(cg, [], {}, _llm_returning([]))
    assert reports == []


def test_synthesize_cluster_populates_evidence_and_source_ids():
    cg = _make_cg({"n1": {"path": "auth.py", "start_line": 1, "end_line": 50,
                           "kind": "function", "qualname": "validate_token",
                           "name": "validate_token"}})
    f1 = _make_finding(title="SQL injection", file="auth.py", line=15)
    f2 = _make_finding(title="Token check removed", severity="critical",
                       file="auth.py", line=10)
    cr = _make_cluster_review(members=("n1",), findings=[f2])
    payload = [{
        "title": "Auth issue",
        "severity": "critical",
        "root_cause_file": "auth.py",
        "root_cause_line": 10,
        "root_cause_summary": "s",
        "exploit_path": "e",
        "impact_chain": "a → b",
        "affected_files": ["api.py"],
        "recommendation": "r",
        "confidence": "high",
    }]
    reports = synthesize_cluster(cg, cr, {"auth.py": [f1]}, _llm_returning(payload), cluster_idx=0)
    assert len(reports) == 1
    r = reports[0]
    assert "auth.py" in r.evidence_by_file
    assert len(r.source_finding_ids) == 2


def test_synthesize_all_no_loss_adds_unassigned_bucket():
    """If LLM reports drop findings, an explicit Unassigned Findings issue is added."""
    cg = _make_cg({
        "n1": {"path": "auth.py", "start_line": 1, "end_line": 20,
               "kind": "function", "qualname": "f1", "name": "f1"},
    })
    # Local finding exists, but LLM returns no issues
    local = _make_finding(title="Missing auth", severity="high", file="auth.py", line=5)
    cr = _make_cluster_review(members=("n1",), findings=[])

    def complete(system, user):
        return "[]"

    reports = synthesize_all(cg, [cr], {"auth.py": [local]}, complete, max_workers=1)
    assert reports, "Expected at least unassigned bucket"
    bucket = [r for r in reports if r.is_unassigned_bucket]
    assert len(bucket) == 1
    assert finding_id(local) in bucket[0].unassigned_ids
    assert any(f.title == "Missing auth" for f in bucket[0].source_findings)
