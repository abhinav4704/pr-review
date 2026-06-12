"""Tests for eval.py — judge index semantics, valid_indices validation."""

import json
from unittest.mock import MagicMock

import pytest

from pr_review.eval import ExpectedFinding, _judge


def _fake_nova(response: dict):
    nova = MagicMock()
    nova.complete_json.return_value = response
    return nova


EXPECTED = ExpectedFinding(
    file="app.py",
    line_range=(10, 20),
    category="security",
    min_severity="high",
    description="SQL injection via f-string",
)

ACTUALS = [
    {"index": 5, "category": "security", "file": "app.py", "line": 15,
     "severity": "high", "title": "SQL injection", "explanation": "..."},
    {"index": 7, "category": "correctness", "file": "app.py", "line": 30,
     "severity": "medium", "title": "Null dereference", "explanation": "..."},
]
VALID_INDICES = {5, 7}


def test_judge_matched_returns_correct_global_index():
    nova = _fake_nova({"matched": True, "matched_index": 5, "reason": "matches sqli"})
    matched, idx, reason = _judge(EXPECTED, ACTUALS, nova, valid_indices=VALID_INDICES)
    assert matched is True
    assert idx == 5


def test_judge_invalid_index_discarded():
    """If judge returns a filtered-list position (0) instead of global index,
    and 0 is not in valid_indices, it should be discarded."""
    nova = _fake_nova({"matched": True, "matched_index": 0, "reason": "found it"})
    # 0 is not in VALID_INDICES {5, 7}
    matched, idx, reason = _judge(EXPECTED, ACTUALS, nova, valid_indices=VALID_INDICES)
    assert matched is True
    assert idx is None, "Index 0 is not a valid global index and must be discarded"


def test_judge_not_matched():
    nova = _fake_nova({"matched": False, "matched_index": None, "reason": "no match"})
    matched, idx, reason = _judge(EXPECTED, ACTUALS, nova, valid_indices=VALID_INDICES)
    assert matched is False
    assert idx is None


def test_judge_parse_error_returns_false():
    nova = _fake_nova(None)  # complete_json returns None
    matched, idx, reason = _judge(EXPECTED, ACTUALS, nova)
    assert matched is False
    assert "parse error" in reason


def test_judge_non_integer_index_discarded():
    nova = _fake_nova({"matched": True, "matched_index": "not-an-int", "reason": "ok"})
    matched, idx, reason = _judge(EXPECTED, ACTUALS, nova, valid_indices=VALID_INDICES)
    assert matched is True
    assert idx is None
