"""Tests for llm.py — _extract_json and NovaClient.normalize_blocks."""

import pytest
from pr_review.llm import _extract_json, NovaClient


# ── _extract_json ─────────────────────────────────────────────────────────────

def test_fenced_json_array():
    text = '```json\n[{"a": 1}]\n```'
    result = _extract_json(text)
    assert result == [{"a": 1}]


def test_fenced_json_no_lang():
    text = '```\n{"key": "value"}\n```'
    result = _extract_json(text)
    assert result == {"key": "value"}


def test_bare_json():
    result = _extract_json('[{"b": 2}]')
    assert result == [{"b": 2}]


def test_embedded_json_extracted():
    text = 'Here is the result: [{"x": 99}] and some trailing text.'
    result = _extract_json(text)
    assert result == [{"x": 99}]


def test_no_json_returns_none():
    assert _extract_json("no json here") is None


def test_broken_json_returns_none():
    assert _extract_json("[{broken}]") is None


# ── normalize_blocks ──────────────────────────────────────────────────────────

def test_normalize_blocks_is_on_class():
    """normalize_blocks must be a method on NovaClient, not a module-level function."""
    assert hasattr(NovaClient, "normalize_blocks")


def test_normalize_blocks_text():
    raw = [{"text": "hello"}]
    out = NovaClient.normalize_blocks(raw)
    assert out == [{"type": "text", "text": "hello"}]


def test_normalize_blocks_tool_use():
    raw = [{"toolUse": {"toolUseId": "tu-1", "name": "get_callers", "input": {"node_id": "n"}}}]
    out = NovaClient.normalize_blocks(raw)
    assert len(out) == 1
    assert out[0]["type"] == "tool_use"
    assert out[0]["id"] == "tu-1"
    assert out[0]["name"] == "get_callers"


def test_normalize_blocks_mixed():
    raw = [
        {"text": "thinking..."},
        {"toolUse": {"toolUseId": "tu-2", "name": "get_source", "input": {}}},
        {"text": "findings: []"},
    ]
    out = NovaClient.normalize_blocks(raw)
    assert len(out) == 3
    assert out[0]["type"] == "text"
    assert out[1]["type"] == "tool_use"
    assert out[2]["type"] == "text"
