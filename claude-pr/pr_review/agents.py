"""Specialist review agents with an agentic tool loop.

Each agent gets a focused system prompt for its domain plus a set of tools
it can call to query the code graph and embedding index mid-reasoning.

Agents:
    SecurityAgent       — secrets, injections, auth, crypto, OWASP
    CorrectnessAgent    — bugs, regressions, null/edge-case, type errors
    PerformanceAgent    — N+1, O(n²), unbounded loops, blocking calls
    ApiContractAgent    — route signature changes, schema breaks, versioning
    TestCoverageAgent   — missing tests, untested branches, weak assertions
    ArchitectureAgent   — coupling, layer violations, convention drift

Tool loop: each agent can call up to MAX_TOOL_ROUNDS graph / embedding
queries to retrieve additional context before writing its final findings.
The loop terminates when the agent emits findings JSON or hits the round limit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .graph import CodeGraph
from .llm import NovaClient

if TYPE_CHECKING:
    from .embeddings import EmbeddingIndex

MAX_TOOL_ROUNDS = 2

# ── Tool definitions sent to agents ───────────────────────────────────────────
TOOLS = [
    {
        "name": "get_callers",
        "description": "Return all nodes that call a given node id (up to 2 hops).",
        "input_schema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "The node id to look up."}
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "get_callees",
        "description": "Return all nodes called by a given node id (1 hop).",
        "input_schema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "The node id to look up."}
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "get_source",
        "description": "Fetch the exact source code of a node by its node id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "Node id (path::qualname)."}
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "find_similar",
        "description": "Semantic search: find code chunks in the repo similar to a text query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language or code snippet."},
                "top_k": {"type": "integer", "description": "Number of results (default 4)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_routes",
        "description": "List all HTTP route nodes in the repo (FastAPI/Flask/etc).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_tables",
        "description": "List all database table/model nodes in the repo.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

FINDINGS_SCHEMA = """
Return ONLY a JSON array (no prose, no fences) where each element has:
{
  "kind": "issue"|"suggestion", // "issue" = something wrong; "suggestion" = optional improvement
  "category": string,       // agent's specialty category
  "severity": "critical"|"high"|"medium"|"low"|"info",
  "file": string,           // file path
  "line": integer,          // most relevant line number
  "title": string,          // one short sentence
  "explanation": string,    // 2-4 sentences reasoning about impact
  "evidence": string,       // short snippet that demonstrates the issue
  "recommendation": string  // concrete actionable fix
}
Report concrete problems as "issue". Report optional improvements (style,
refactors, nice-to-haves) as "suggestion" with severity "info" or "low".
If you find nothing, return [].
"""


# ── Tool executor ──────────────────────────────────────────────────────────────
def execute_tool(name: str, inputs: Dict[str, Any],
                 cg: CodeGraph,
                 embed_index: Optional["EmbeddingIndex"]) -> str:
    try:
        if name == "get_callers":
            nid = inputs["node_id"]
            if not cg.has(nid):
                return json.dumps({"error": f"node {nid!r} not found"})
            callers = cg.callers(nid)
            return json.dumps({"callers": callers[:20]})

        elif name == "get_callees":
            nid = inputs["node_id"]
            if not cg.has(nid):
                return json.dumps({"error": f"node {nid!r} not found"})
            callees = cg.callees(nid)
            return json.dumps({"callees": callees[:20]})

        elif name == "get_source":
            nid = inputs["node_id"]
            if not cg.has(nid):
                return json.dumps({"error": f"node {nid!r} not found"})
            src = cg.source(nid)
            return json.dumps({"source": src[:3000]})

        elif name == "find_similar":
            if embed_index is None or not embed_index._ready:
                return json.dumps({"results": [], "note": "embedding index not built"})
            query = inputs.get("query", "")
            top_k = int(inputs.get("top_k", 4))
            results = embed_index.find_similar_patterns(query, top_k=top_k)
            return json.dumps({"results": results[:top_k]})

        elif name == "get_routes":
            routes = cg.routes()
            route_info = []
            for r in routes[:30]:
                d = cg.node(r)
                route_info.append({
                    "node_id": r,
                    "path": d.get("route_path", ""),
                    "method": d.get("route_method", ""),
                    "file": d.get("path", ""),
                    "line": d.get("start_line", 0),
                })
            return json.dumps({"routes": route_info})

        elif name == "get_tables":
            tables = cg.tables()
            tbl_info = []
            for t in tables[:30]:
                d = cg.node(t)
                tbl_info.append({
                    "node_id": t,
                    "name": d.get("name", ""),
                    "file": d.get("path", ""),
                    "line": d.get("start_line", 0),
                })
            return json.dumps({"tables": tbl_info})

        else:
            return json.dumps({"error": f"unknown tool {name!r}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Base agent ─────────────────────────────────────────────────────────────────
@dataclass
class Finding:
    category: str
    severity: str
    file: str
    line: int
    title: str
    explanation: str
    evidence: str
    recommendation: str
    kind: str = "issue"          # "issue" | "suggestion"

    @classmethod
    def from_dict(cls, d: dict) -> Optional["Finding"]:
        try:
            severity = str(d.get("severity", "low")).lower()
            kind = str(d.get("kind", "")).lower()
            if kind not in ("issue", "suggestion"):
                # fall back: treat informational findings as suggestions
                kind = "suggestion" if severity == "info" else "issue"
            return cls(
                category=str(d.get("category", "code-quality")),
                severity=severity,
                file=str(d.get("file", "")),
                line=int(d.get("line", 0) or 0),
                title=str(d.get("title", "")).strip(),
                explanation=str(d.get("explanation", "")).strip(),
                evidence=str(d.get("evidence", "")).strip(),
                recommendation=str(d.get("recommendation", "")).strip(),
                kind=kind,
            )
        except (TypeError, ValueError):
            return None


def _parse_findings(text: str) -> List[Finding]:
    from .llm import _extract_json
    raw = _extract_json(text)
    if not isinstance(raw, list):
        return []
    out: List[Finding] = []
    for item in raw:
        if isinstance(item, dict):
            f = Finding.from_dict(item)
            if f and f.title:
                out.append(f)
    return out


class BaseAgent:
    name: str = "base"
    system_prompt: str = ""
    category: str = "code-quality"

    def run(self, dossier, cg, embed_index, nova):
        messages = [{"role": "user", "content": [{"text": self._user_prompt(dossier)}]}]
        findings: List[Finding] = []

        for _round in range(MAX_TOOL_ROUNDS + 1):
            raw_content = nova.converse_with_tools(self.system_prompt, messages, TOOLS)
            blocks = NovaClient.normalize_blocks(raw_content)

            tool_uses = [b for b in blocks if b["type"] == "tool_use"]
            for b in blocks:
                if b["type"] == "text":
                    findings.extend(_parse_findings(b["text"]))

            if not tool_uses:
                break

            messages.append({"role": "assistant", "content": raw_content})
            messages.append({"role": "user", "content": [
                {"toolResult": {
                    "toolUseId": tu["id"],
                    "content": [{"text": execute_tool(tu["name"], tu["input"], cg, embed_index)}],
                }}
                for tu in tool_uses
            ]})

        # Fallback: tools were used but no findings emitted. Give one more turn to
        # respond to the pending tool results. No new message is appended (that would
        # break role alternation), and TOOLS stays passed so toolConfig remains valid.
        if not findings and len(messages) > 1 and messages[-1]["role"] == "user":
            for b in NovaClient.normalize_blocks(
                nova.converse_with_tools(self.system_prompt, messages, TOOLS)
            ):
                if b["type"] == "text":
                    findings.extend(_parse_findings(b["text"]))

        # Dedup within this agent's run to suppress restated partials across rounds.
        seen: set = set()
        deduped: List[Finding] = []
        for f in findings:
            key = (f.file, f.line, f.title[:40])
            if key not in seen:
                seen.add(key)
                deduped.append(f)
        return deduped
    
    def _user_prompt(self, dossier: str) -> str:
        return (
            f"You are the {self.name} specialist. Below is the FULL content of a file "
            f"under review (or one chunk of a large file). Lines added or changed in "
            f"this PR are marked with a leading '+'.\n\n"
            f"Focus ONLY on {self.category} issues. Verify the code works as a whole: "
            f"PRIORITIZE the changed (+) lines and anything they affect, but you may also "
            f"report a serious bug elsewhere in the file. Pin every finding to a line number.\n"
            f"Use the provided tools if you need to look up callers, source, or "
            f"similar patterns before making a judgment.\n\n"
            f"When you have finished your analysis, output your findings as JSON:\n"
            f"{FINDINGS_SCHEMA}\n\n"
            f"=== FILE UNDER REVIEW ===\n{dossier}"
        )


# ── Specialist agents ──────────────────────────────────────────────────────────
class SecurityAgent(BaseAgent):
    name = "security"
    category = "security, secret/credential"
    system_prompt = (
        "You are a senior application security engineer performing a security-focused "
        "code review. You look for: hardcoded secrets/credentials/API keys, SQL/command "
        "injection, broken authentication, insecure direct object reference, missing "
        "authorization checks, unsafe deserialization, weak cryptography (MD5, SHA1, "
        "ECB mode), exposed PII, path traversal, SSRF, open redirects, and OWASP Top 10. "
        "Only report issues with direct evidence in the provided code. Do not speculate."
    )


class CorrectnessAgent(BaseAgent):
    name = "correctness"
    category = "bug/regression"
    system_prompt = (
        "You are a senior engineer focused on correctness. You look for: null/None "
        "dereferences, off-by-one errors, incorrect operator precedence, wrong "
        "comparison (= vs ==), unchecked return values, resource leaks (file/socket/"
        "DB connection not closed), race conditions, integer overflow, incorrect "
        "exception handling, and regressions — cases where the changed code breaks "
        "a contract that callers rely on. Use tools to inspect callers if unsure."
    )


class PerformanceAgent(BaseAgent):
    name = "performance"
    category = "performance"
    system_prompt = (
        "You are a performance-focused engineer. You look for: N+1 database queries "
        "(loop containing DB call), O(n²) or worse algorithms where O(n log n) exists, "
        "unbounded in-memory accumulation, missing indexes (inferred from query patterns), "
        "synchronous blocking calls in async contexts, redundant re-computation, "
        "missing caching for expensive operations, and unnecessary serialization. "
        "Only flag issues where the evidence is visible in the provided code."
    )


class ApiContractAgent(BaseAgent):
    name = "api_contract"
    category = "api-contract, database"
    system_prompt = (
        "You are a senior API and database contract reviewer. You look for: "
        "breaking changes to function signatures that have external callers, "
        "HTTP route path/method changes that break existing clients, "
        "response schema changes (added required fields, removed fields, type changes), "
        "database migration risks (column renames, type changes, NOT NULL without default), "
        "missing backward-compatible defaults on new parameters, and "
        "versioning violations. Use get_routes and get_tables tools to find all "
        "API routes and DB tables in the repo when relevant."
    )


class TestCoverageAgent(BaseAgent):
    name = "test_coverage"
    category = "test-coverage"
    system_prompt = (
        "You are a test quality engineer. You look for: changed functions with no "
        "covering tests at all, branches/edge cases in the changed code that have "
        "no test, tests that exist but are too weak (only happy path, no error cases), "
        "missing integration tests for route handlers, and test setup that may be "
        "incorrect after the change. Use get_callers and find_similar to check if "
        "tests exist elsewhere in the repo."
    )


class ArchitectureAgent(BaseAgent):
    name = "architecture"
    category = "architecture, maintainability, dependency"
    system_prompt = (
        "You are a software architect reviewing for structural quality. You look for: "
        "circular imports, inappropriate layer violations (e.g. a model importing from "
        "a controller), tight coupling where dependency injection should be used, "
        "magic numbers/strings that should be constants, duplicated logic that already "
        "exists in the repo (use find_similar to check), overly long functions (>50 lines "
        "of added code), and deviation from established patterns in the codebase. "
        "Emit structural improvements that are optional as kind=\"suggestion\"; emit "
        "genuine defects (e.g. circular imports that will fail) as kind=\"issue\"."
    )


ALL_AGENTS: List[BaseAgent] = [
    SecurityAgent(),
    CorrectnessAgent(),
    PerformanceAgent(),
    ApiContractAgent(),
    TestCoverageAgent(),
    ArchitectureAgent(),
]

# Registry keyed by agent.name, used by profiles.build_agents().
AGENT_REGISTRY: Dict[str, type] = {
    "security": SecurityAgent,
    "correctness": CorrectnessAgent,
    "performance": PerformanceAgent,
    "api_contract": ApiContractAgent,
    "test_coverage": TestCoverageAgent,
    "architecture": ArchitectureAgent,
}


# ── Breaking-change (caller compatibility) pass ────────────────────────────────
# Used by review.run_dependency_check — not an agentic tool-loop agent. Given a
# changed function's identity card (old -> new) and its UNMODIFIED call sites in
# other files, decide for each whether the change breaks it.
DEPENDENCY_SYSTEM = (
    "You are a senior engineer checking whether a change to one function breaks "
    "the code in OTHER files that calls it. You are given the changed function's "
    "identity (old vs new signature/behavior) and the exact call sites that were "
    "NOT modified in this PR. For each call site, decide if it is now broken — "
    "wrong number/names/types of arguments, a removed/renamed parameter, a changed "
    "return type/shape the caller still uses the old way, or a behavioral contract "
    "the caller relies on. Report ONLY call sites that will actually break. Do not "
    "speculate about call sites that still look compatible."
)

DEPENDENCY_INSTRUCTIONS = """A function changed in this PR. Decide which UNMODIFIED callers it breaks.

{schema}

Use category "breaking-change" and kind "issue". Severity "critical" if it will
raise/crash or corrupt data, otherwise "high". Set "file" and "line" to the exact
caller call site. In "explanation" state plainly: "changing X breaks this call
because ...; this caller was not updated in the PR."

=== CHANGED FUNCTION ===
{card}

=== UNMODIFIED CALL SITES (in other files) ===
{callers}
"""
