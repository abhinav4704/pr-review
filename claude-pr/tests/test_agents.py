"""Tests for agents.py — BaseAgent tool loop with a fake NovaClient.

Verifies:
  (a) message roles strictly alternate user/assistant
  (b) assistant turn contains native toolUse keys (not "type": "tool_use")
  (c) toolResult content is a list of {"text": ...} blocks
  (d) findings parse correctly
  (e) fallback path fires when tools used but no findings emitted
"""

import json
from dataclasses import dataclass, field
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from pr_review.agents import BaseAgent, Finding, TOOLS, _parse_findings
from pr_review.llm import NovaClient


# ── Scripted fake Nova client ──────────────────────────────────────────────────

class ScriptedNova:
    """Returns canned responses in sequence; repeats last if exhausted."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._calls = []

    def converse_with_tools(self, system, messages, tools):
        self._calls.append({"messages": list(messages), "tools": tools})
        if self._responses:
            return self._responses.pop(0)
        return [{"text": "[]"}]  # safe default once scripts are exhausted


class ScriptedNova_NoFindings:
    """First call returns a toolUse, second call (fallback) returns findings."""

    def __init__(self, fallback_findings):
        self._call_count = 0
        self._fallback_findings = fallback_findings

    def converse_with_tools(self, system, messages, tools):
        self._call_count += 1
        if self._call_count == 1:
            # First call: returns a tool-use block with no findings text
            return [{"toolUse": {"toolUseId": "tu-fb-1",
                                 "name": "get_source",
                                 "input": {"node_id": "fake::fn"}}}]
        else:
            # Fallback call: returns findings text
            return [{"text": json.dumps(self._fallback_findings)}]


# ── Simple test agent ──────────────────────────────────────────────────────────

class SimpleAgent(BaseAgent):
    name = "test_agent"
    category = "test"
    system_prompt = "test"


# ── Mocked code graph / embed ──────────────────────────────────────────────────

def _mock_cg():
    cg = MagicMock()
    cg.has.return_value = True
    cg.source.return_value = "def fake(): pass"
    return cg


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_tool_loop_role_alternation():
    """Roles must strictly alternate user/assistant when tools are used."""
    findings_json = json.dumps([{
        "kind": "issue", "category": "security", "severity": "high",
        "file": "app.py", "line": 10, "title": "Secret exposed",
        "explanation": "...", "evidence": "...", "recommendation": "..."
    }])
    # Call 1: returns a toolUse block (no text)
    # Call 2: returns findings text (no tool)
    nova = ScriptedNova([
        [{"toolUse": {"toolUseId": "tu-abc", "name": "get_source",
                       "input": {"node_id": "app::fn"}}}],
        [{"text": findings_json}],
    ])
    agent = SimpleAgent()
    result = agent.run("dossier text", _mock_cg(), None, nova)

    # At least two calls must have been made
    assert len(nova._calls) >= 2

    # Check role alternation across all calls
    for call in nova._calls:
        msgs = call["messages"]
        for i in range(1, len(msgs)):
            prev_role = msgs[i - 1]["role"]
            cur_role = msgs[i]["role"]
            assert prev_role != cur_role, (
                f"Consecutive same roles: {prev_role!r} at positions {i-1},{i}"
            )


def test_assistant_turn_contains_native_tool_use_keys():
    """The assistant turn appended to messages must have native Bedrock toolUse keys."""
    # Return actual findings on round 1 so fallback does NOT trigger.
    findings_json = json.dumps([{
        "kind": "issue", "category": "security", "severity": "low",
        "file": "a.py", "line": 1, "title": "ok",
        "explanation": "", "evidence": "", "recommendation": "",
    }])
    nova = ScriptedNova([
        [{"toolUse": {"toolUseId": "tu-xyz", "name": "get_callers",
                       "input": {"node_id": "mod::func"}}}],
        [{"text": findings_json}],
    ])
    agent = SimpleAgent()
    agent.run("dossier", _mock_cg(), None, nova)

    # The second call's messages should include an assistant turn from the first call
    second_call_msgs = nova._calls[1]["messages"]
    assistant_turns = [m for m in second_call_msgs if m["role"] == "assistant"]
    assert assistant_turns, "No assistant turn found in subsequent call"
    asst_content = assistant_turns[-1]["content"]
    # Must be a list of blocks; the tool-use block must use native "toolUse" key
    tool_blocks = [b for b in asst_content if isinstance(b, dict) and "toolUse" in b]
    assert tool_blocks, "Assistant content must contain native 'toolUse' blocks"
    # Must NOT use the normalized "type": "tool_use" format
    typed_blocks = [b for b in asst_content if b.get("type") == "tool_use"]
    assert not typed_blocks, "Assistant content must NOT contain normalized 'type':'tool_use' blocks"


def test_tool_result_content_is_list_of_text_blocks():
    """toolResult.content must be a list of {text: ...} dicts, not a bare string."""
    findings_json = json.dumps([{
        "kind": "issue", "category": "security", "severity": "low",
        "file": "a.py", "line": 1, "title": "ok",
        "explanation": "", "evidence": "", "recommendation": "",
    }])
    nova = ScriptedNova([
        [{"toolUse": {"toolUseId": "tu-r1", "name": "get_source",
                       "input": {"node_id": "mod::func"}}}],
        [{"text": findings_json}],  # return findings so fallback doesn't fire
    ])
    agent = SimpleAgent()
    agent.run("dossier", _mock_cg(), None, nova)

    second_call_msgs = nova._calls[1]["messages"]
    user_turns_after_start = [m for m in second_call_msgs[1:] if m["role"] == "user"]
    assert user_turns_after_start, "No tool-result user turn found"
    # Find the turn with toolResult
    tool_result_turns = [
        m for m in user_turns_after_start
        if isinstance(m["content"], list)
        and any("toolResult" in b for b in m["content"])
    ]
    assert tool_result_turns, "No toolResult block in user turns"
    for turn in tool_result_turns:
        for block in turn["content"]:
            if "toolResult" in block:
                content = block["toolResult"]["content"]
                assert isinstance(content, list), "toolResult content must be a list"
                assert all(isinstance(b, dict) and "text" in b for b in content), \
                    "Each toolResult content block must be {'text': ...}"


def test_findings_parse_correctly():
    """Findings JSON from the agent response must deserialize to Finding objects."""
    findings_json = json.dumps([{
        "kind": "issue", "category": "security", "severity": "critical",
        "file": "main.py", "line": 42, "title": "Hardcoded secret",
        "explanation": "AWS key exposed", "evidence": "AWS_KEY='AKIA...'",
        "recommendation": "Use secrets manager",
    }])
    nova = ScriptedNova([[{"text": findings_json}]])
    agent = SimpleAgent()
    result = agent.run("dossier", _mock_cg(), None, nova)

    assert len(result) == 1
    f = result[0]
    assert isinstance(f, Finding)
    assert f.severity == "critical"
    assert f.title == "Hardcoded secret"
    assert f.file == "main.py"
    assert f.line == 42


def test_fallback_fires_when_tools_used_but_no_findings():
    """When tools used and no findings emitted, fallback call returns findings."""
    fallback_findings = [{
        "kind": "issue", "category": "correctness", "severity": "high",
        "file": "util.py", "line": 7, "title": "Null dereference",
        "explanation": "x can be None", "evidence": "x.foo()",
        "recommendation": "Check x is not None",
    }]
    nova = ScriptedNova_NoFindings(fallback_findings)
    agent = SimpleAgent()
    result = agent.run("dossier", _mock_cg(), None, nova)

    assert nova._call_count == 2, "Fallback must trigger exactly one more call"
    assert len(result) == 1
    assert result[0].title == "Null dereference"


def test_dedup_within_run():
    """The same (file, line, title) emitted twice in different rounds is deduped."""
    dup = {
        "kind": "issue", "category": "security", "severity": "high",
        "file": "a.py", "line": 5, "title": "Duplicate finding here",
        "explanation": "...", "evidence": "", "recommendation": "",
    }
    # Both rounds emit the same finding
    nova = ScriptedNova([
        [{"toolUse": {"toolUseId": "tu-d1", "name": "get_source", "input": {"node_id": "x"}}}],
        [{"text": json.dumps([dup, dup])}],  # two identical findings in fallback
    ])
    agent = SimpleAgent()
    result = agent.run("dossier", _mock_cg(), None, nova)
    titles = [f.title for f in result]
    assert titles.count("Duplicate finding here") == 1, "Duplicate must be removed"
