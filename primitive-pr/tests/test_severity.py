"""Tests for the light-guard on 'critical' severity (findings.py + synthesis.py).

The model's "critical" is trusted, with one light guard:
  - raw finding: a "critical" with no evidence is trimmed to high
  - synthesized issue: a "critical" the model is not highly confident about -> high
Everything else (incl. high-confidence / evidence-backed criticals of any category)
is left as the model called it.
"""

from __future__ import annotations

import json

from pr_review.findings import Finding, cap_finding_severity, parse_findings
from pr_review.synthesis import IssueReport, finalize_report_severity


def _finding(**kw) -> Finding:
    base = dict(
        category="bug", severity="critical", file="app.py", line=5,
        title="bad thing", explanation="why", evidence="x()",
        recommendation="fix",
    )
    base.update(kw)
    return Finding(**base)


def _report(**kw) -> IssueReport:
    base = dict(
        title="t", severity="critical", root_cause_file="schema.py", root_cause_line=1,
        root_cause_summary="s", exploit_path="", impact_chain="", affected_files=[],
        recommendation="fix", confidence="high",
    )
    base.update(kw)
    return IssueReport(**base)


# ── raw findings ──────────────────────────────────────────────────────────────

def test_critical_with_evidence_stays_critical_any_category():
    assert cap_finding_severity(_finding(category="bug")).severity == "critical"
    assert cap_finding_severity(_finding(category="vulnerability")).severity == "critical"
    assert cap_finding_severity(_finding(category="breaking")).severity == "critical"


def test_critical_without_evidence_drops_to_high():
    assert cap_finding_severity(_finding(evidence="")).severity == "high"
    assert cap_finding_severity(_finding(evidence="   ")).severity == "high"


def test_non_critical_untouched():
    assert cap_finding_severity(_finding(severity="high", evidence="")).severity == "high"


def test_parse_findings_applies_light_guard():
    text = json.dumps([
        {"category": "bug", "severity": "critical", "file": "a.py", "line": 1,
         "title": "supported", "evidence": "y()", "recommendation": "fix"},
        {"category": "bug", "severity": "critical", "file": "a.py", "line": 2,
         "title": "unsupported", "evidence": "", "recommendation": "fix"},
    ])
    out = {f.title: f.severity for f in parse_findings(text)}
    assert out["supported"] == "critical"
    assert out["unsupported"] == "high"


# ── synthesized issue reports ─────────────────────────────────────────────────

def test_report_high_conf_stays_critical():
    r = _report(confidence="high")
    finalize_report_severity(r)
    assert r.severity == "critical"


def test_report_low_or_medium_conf_drops_to_high():
    for conf in ("medium", "low"):
        r = _report(confidence=conf)
        finalize_report_severity(r)
        assert r.severity == "high"


def test_report_non_critical_untouched():
    r = _report(severity="high", confidence="low")
    finalize_report_severity(r)
    assert r.severity == "high"
