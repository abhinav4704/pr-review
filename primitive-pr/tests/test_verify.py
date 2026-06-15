"""Verification pass must PRUNE, never WIPE — it can't silently delete real
(often cross-file) breakages when the verifier returns nothing usable."""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pr_review.pr_passes import verify_findings
from pr_review.findings import Finding


def _f(title, file="a.py", line=6):
    return Finding(category="breaking", severity="critical", file=file, line=line,
                   title=title, explanation="", evidence="", recommendation="")


FINDINGS = [_f("cross-file break A", "a.py", 6), _f("speculative B", "b.py", 9)]


def test_verifier_prunes_to_subset():
    def keep_a(_s, _u):
        return json.dumps([{"category": "breaking", "severity": "critical",
                            "file": "a.py", "line": 6, "title": "cross-file break A",
                            "explanation": "", "evidence": "", "recommendation": ""}])
    out = verify_findings(keep_a, "DOSSIER", FINDINGS, "a.py")
    assert [f.title for f in out] == ["cross-file break A"]


def test_empty_verifier_keeps_originals_not_wipe():
    out = verify_findings(lambda s, u: "[]", "DOSSIER", FINDINGS, "a.py")
    assert {f.title for f in out} == {"cross-file break A", "speculative B"}


def test_garbage_verifier_keeps_originals():
    out = verify_findings(lambda s, u: "sorry, no json", "DOSSIER", FINDINGS, "a.py")
    assert {f.title for f in out} == {"cross-file break A", "speculative B"}


def test_rephrased_title_matched_by_location():
    def rephrase(_s, _u):
        return json.dumps([{"category": "breaking", "severity": "critical",
                            "file": "a.py", "line": 6, "title": "A crashes at runtime",
                            "explanation": "", "evidence": "", "recommendation": ""}])
    out = verify_findings(rephrase, "DOSSIER", FINDINGS, "a.py")
    assert [(f.file, f.line) for f in out] == [("a.py", 6)]


def test_findings_kept_whole_in_single_call():
    """The findings JSON must reach the verifier in one piece (no chunk split)."""
    seen = {}

    def capture(_s, user):
        seen["user"] = user
        return "[]"
    verify_findings(capture, "DOSSIER " * 5000, FINDINGS, "a.py", budget=4000)
    assert "cross-file break A" in seen["user"]
    assert "speculative B" in seen["user"]
