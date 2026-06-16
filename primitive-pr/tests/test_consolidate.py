"""The final consolidation pass must FUSE a file's whole-file review with the
impact chains it's part of — and, like verification, never silently wipe findings
when the model returns nothing usable."""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pr_review.pr_passes import (
    consolidate_file,
    consolidate_by_file,
    ClusterReview,
)
from pr_review.findings import Finding
from pr_review.impact import Chain, ChainNode, Cluster


def _cn(path, name, role, changed, start=1, end=5, kind="function"):
    return ChainNode(
        node_id=f"{path}::{name}", role=role, changed=changed,
        change_type="signature" if changed else None, modified_in_pr=False,
        kind=kind, qualname=name, path=path, start_line=start, end_line=end,
        is_test=False)


def _f(title, file, line=2, sev="critical"):
    return Finding(category="breaking", severity=sev, file=file, line=line,
                   title=title, explanation="", evidence="", recommendation="")


# schema (changed source) -> analyze (unchanged consumer, still reads .response)
SRC = _cn("schema.py", "Schema", "source", True)
CONSUMER = _cn("insights.py", "analyze", "consumer", False, start=1, end=10)
CHAIN = Chain(nodes=[SRC, CONSUMER], consumer_id=CONSUMER.node_id, distance=1,
              field_hits=["response"])
TRACK_B = _f("analyze reads removed field response", "insights.py", line=2)
REVIEWS = [ClusterReview(cluster=Cluster(members=[SRC.node_id], chains=[CHAIN]),
                         findings=[TRACK_B])]
TRACK_A = {"schema.py": [_f("Schema field rename", "schema.py", line=3)]}


def test_consolidate_file_merges_inputs():
    """The model sees BOTH the whole-file finding and the chain finding as candidates."""
    captured = {}

    def stub(_system, user):
        captured["user"] = user
        return json.dumps([{
            "category": "breaking", "severity": "critical", "file": "insights.py",
            "line": 2, "title": "Schema rename breaks analyze",
            "explanation": "merged", "impact": "AttributeError on .response",
            "evidence": "r.response", "recommendation": "use .output_text"}])

    out = consolidate_file("schema.py", ".", {"schema.py": "@@\n-resp\n+out\n"},
                           TRACK_A, REVIEWS, stub, renames={"response": "output_text"})
    assert [f.title for f in out] == ["Schema rename breaks analyze"]
    # both the whole-file finding and the chain finding were offered as candidates
    assert "Schema field rename" in captured["user"]
    assert "analyze reads removed field response" in captured["user"]
    # the cross-file CAUSE context was included
    assert "this file breaks" in captured["user"]


def test_consolidate_file_falls_back_on_empty():
    """Empty/garbage model output must fall back to the union of inputs, not wipe."""
    out = consolidate_file("schema.py", ".", {}, TRACK_A, REVIEWS,
                           lambda s, u: "", renames={"response": "output_text"})
    titles = {f.title for f in out}
    assert "Schema field rename" in titles                 # whole-file kept
    assert "analyze reads removed field response" in titles  # chain finding kept


def test_consolidate_file_no_candidates_returns_empty():
    out = consolidate_file("untouched.py", ".", {}, {}, REVIEWS, lambda s, u: "[]")
    assert out == []


def test_consolidate_by_file_covers_cause_and_victim():
    """Both the source file and the consumer file get a consolidated entry."""
    out = consolidate_by_file(
        cg=None, src_path=".", file_diffs=[], diff_by_file={},
        track_a=TRACK_A, impact_reviews=REVIEWS, complete=lambda s, u: "",
        renames={"response": "output_text"})
    assert "schema.py" in out       # the cause
    assert "insights.py" in out     # the victim
    # victim side keeps the chain finding via fallback
    assert any("response" in f.title for f in out["insights.py"])
