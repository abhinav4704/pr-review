"""Tests for review.py — syntax gate, skip hole, risk floor, error visibility."""

import ast
import types
from dataclasses import dataclass, field
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from pr_review.agents import Finding
from pr_review.diff import FileDiff, parse_diff
from pr_review.review import (
    OverallResult,
    _risk,
    _syntax_check,
    run_file_review,
    run_review,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

BROKEN_SRC = "BROKEN_LABELGEN_STATE ="  # bare assignment — SyntaxError


def _make_cg(source: str = "", path: str = "app.py"):
    """Minimal fake CodeGraph."""
    cg = MagicMock()
    cg.source_lines.return_value = source
    cg.has.return_value = False
    cg.node_for_line.return_value = None
    cg.g.nodes.return_value = []
    return cg


def _make_fd(path: str = "app.py", added_lines=None, is_new=False, is_deleted=False):
    al = set(added_lines or [1])
    return FileDiff(path=path, is_new=is_new, is_deleted=is_deleted,
                    added_lines=al, changed_lines=al)


# ── _syntax_check ─────────────────────────────────────────────────────────────

def test_syntax_check_broken_file_returns_critical():
    cg = _make_cg(source=BROKEN_SRC)
    fd = _make_fd(path="broken.py", added_lines={1})
    findings = _syntax_check(cg, fd)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "critical"
    assert f.category == "syntax"
    assert f.kind == "issue"
    assert "broken.py" == f.file


def test_syntax_check_clean_file_returns_empty():
    cg = _make_cg(source="def foo():\n    pass\n")
    fd = _make_fd(path="clean.py", added_lines={1})
    findings = _syntax_check(cg, fd)
    assert findings == []


def test_syntax_check_skips_non_py():
    cg = _make_cg(source=BROKEN_SRC)
    fd = _make_fd(path="index.js", added_lines={1})
    findings = _syntax_check(cg, fd)
    assert findings == []


def test_syntax_check_skips_empty_source():
    cg = _make_cg(source="")
    fd = _make_fd(path="app.py", added_lines={1})
    findings = _syntax_check(cg, fd)
    assert findings == []


# ── _risk floor ───────────────────────────────────────────────────────────────

def test_risk_lone_critical_is_at_least_high():
    """A single critical finding must never be 'low' or 'medium' risk."""
    f = Finding(category="syntax", severity="critical", file="x.py", line=1,
                title="SyntaxError", explanation="", evidence="", recommendation="")
    score, level = _risk({}, [f])
    assert level in ("high", "critical"), f"Expected high or critical, got {level!r}"
    assert score >= 55


def test_risk_clean_file_is_low():
    score, level = _risk({}, [])
    assert level == "low"
    assert score == 0


def test_risk_medium_findings_without_critical():
    findings = [
        Finding("sec", "medium", "x.py", 1, "t", "", "", "")
        for _ in range(3)
    ]
    score, level = _risk({}, findings)
    # 3 × weight(medium=3) = 9 → below 25 → "low" is expected
    assert level == "low"


# ── run_review skip hole ──────────────────────────────────────────────────────

def test_run_review_includes_syntax_broken_file():
    """A file that fails to parse must appear in file_results even if num_chunks=0."""
    # Build a diff with one broken Python file
    diff_text = (
        "diff --git a/broken.py b/broken.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/broken.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+BROKEN_LABELGEN_STATE =\n"
    )
    fds = parse_diff(diff_text)

    # Mock everything the pipeline uses
    cg = _make_cg(source=BROKEN_SRC, path="broken.py")
    # node_for_line returns None → zero chunks
    cg.source_lines.return_value = BROKEN_SRC

    blast = MagicMock()
    blast.metrics = {}
    blast.per_change = {}

    nova = MagicMock()
    nova.complete.return_value = "[]"
    nova.complete_json.return_value = []

    from pr_review.profiles import PROFILES
    profile = PROFILES["quick"]

    # Run with no changes (broken file has no mapped nodes)
    result = run_review(
        cg=cg,
        changes=[],
        blast=blast,
        diff_text=diff_text,
        file_diffs=fds,
        nova=nova,
        profile=profile,
        verify=False,
    )

    assert "broken.py" in result.file_results, (
        "Syntax-broken file must appear in file_results even with num_chunks=0"
    )
    fr = result.file_results["broken.py"]
    assert any(f.severity == "critical" and f.category == "syntax"
               for f in fr.findings), (
        "File must contain a critical syntax finding"
    )


# ── agent error visibility ────────────────────────────────────────────────────

def test_agent_error_recorded_in_agent_runs():
    """An agent that raises must record :ERROR: in agent_runs, not silently pass."""
    from pr_review.review import _review_chunk, ChunkReviewResult
    from pr_review.context import Chunk

    chunk = Chunk(
        file_path="app.py",
        added_lines=[1],
        node_ids=[],
        dossier="test",
        chunk_index=0,
        total_chunks=1,
        start_line=1,
        end_line=5,
    )

    boom_agent = MagicMock()
    boom_agent.name = "boom"
    boom_agent.run.side_effect = RuntimeError("deliberate boom")

    cr = _review_chunk(chunk, MagicMock(), None, MagicMock(), [boom_agent])
    errors = [r for r in cr.agent_runs if ":ERROR:" in r]
    assert errors, "Agent error must appear in agent_runs as 'boom:ERROR:...'"
    assert "boom" in errors[0]


def test_streamlit_app_renders_agent_errors():
    """Verify that streamlit_app.py contains the code to surface agent errors."""
    import pathlib
    src = pathlib.Path(__file__).parent.parent / "streamlit_app.py"
    text = src.read_text(encoding="utf-8")
    assert "agent_errors" in text, "streamlit_app.py must collect agent errors"
    assert ":ERROR:" in text, "streamlit_app.py must check for :ERROR: in agent_runs"
    assert "agent run(s) failed" in text.lower() or "agent error" in text.lower(), (
        "streamlit_app.py must display a banner when agent runs fail"
    )
