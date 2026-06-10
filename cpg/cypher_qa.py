"""LLM-powered Cypher QA over the multiplex graph.

This layer translates a natural-language question into a read-only Cypher query,
executes it against Neo4j, and returns rows plus the generated query.

Safety guarantees:
- Generated Cypher is validated to be read-only before execution.
- Write/destructive clauses are rejected (CREATE/MERGE/DELETE/SET/etc.).
- A LIMIT is enforced when absent.

Usage (programmatic):
    from cpg.cypher_qa import ask_graph
    out = ask_graph(
        question="Which routes are guarded by auth?",
        uri="bolt://localhost:7687",
        user="neo4j",
        password="password",
    )

Environment variables for LLM call:
- OPENAI_API_KEY (required)
- OPENAI_BASE_URL (optional, default https://api.openai.com/v1)
- OPENAI_MODEL (optional, default gpt-4o-mini)
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Any

from neo4j import GraphDatabase

# Graph schema allowlists must stay aligned with store.py.
NODE_KINDS = [
    "File", "Class", "Function", "Route", "FrontendCall",
    "Table", "Lock", "ExternalModule", "ExternalSymbol",
    "Decorator", "Component",
]
REL_TYPES = [
    "CONTAINS", "CALLS", "IMPORTS", "HANDLES", "CALLS_ENDPOINT",
    "READS_TABLE", "WRITES_TABLE", "GUARDED_BY", "ACQUIRES",
    "DECORATES", "DEPENDS_ON", "VALIDATES_WITH",
    "INHERITS", "RAISES", "RENDERS", "USES_COMPONENT",
]

# Disallowed for read-only execution.
_FORBIDDEN = [
    r"\bCREATE\b", r"\bMERGE\b", r"\bDELETE\b", r"\bDETACH\b",
    r"\bSET\b", r"\bREMOVE\b", r"\bDROP\b", r"\bLOAD\s+CSV\b",
    r"\bFOREACH\b", r"\bCALL\s+dbms\.", r"\bCALL\s+apoc\.",
]
_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+\b", re.IGNORECASE)


@dataclass
class QAResult:
    question: str
    cypher: str
    params: dict[str, Any]
    rows: list[dict[str, Any]]
    row_count: int


def ask_graph(
    question: str,
    uri: str,
    user: str,
    password: str,
    database: str = "neo4j",
    *,
    max_rows: int = 200,
) -> QAResult:
    """Generate and execute a read-only Cypher query for a user question."""
    cypher, params = _generate_cypher(question, max_rows=max_rows)
    safe_cypher = _validate_read_only(cypher, max_rows=max_rows)

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            rows = session.run(safe_cypher, params).data()
    finally:
        driver.close()

    return QAResult(
        question=question,
        cypher=safe_cypher,
        params=params,
        rows=rows,
        row_count=len(rows),
    )


def _generate_cypher(question: str, *, max_rows: int) -> tuple[str, dict[str, Any]]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for LLM Cypher generation.")

    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    schema = (
        "Node labels: " + ", ".join(NODE_KINDS) + "\n"
        "Relationship types: " + ", ".join(REL_TYPES) + "\n"
        "Node identity property: id\n"
        "Important node properties often present: name, file, line, line_start, line_end, "
        "method, path, module, resolved"
    )

    system = (
        "You write Neo4j Cypher for a code graph. "
        "Output STRICT JSON only with shape: "
        "{\"cypher\": string, \"params\": object}. "
        "Rules: read-only query only; no CREATE/MERGE/DELETE/SET/DROP. "
        "Use only provided labels and relationship types. "
        f"Include LIMIT <= {max_rows}. "
        "Prefer returning concise columns (name, id, file, method, path, counts)."
    )

    user_prompt = (
        f"Question:\n{question}\n\n"
        f"Schema:\n{schema}\n"
    )

    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
    }

    req = urllib.request.Request(
        url=f"{base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"LLM request failed: {exc}") from exc

    try:
        content = raw["choices"][0]["message"]["content"]
        obj = json.loads(content)
        cypher = str(obj.get("cypher", "")).strip()
        params = obj.get("params", {}) or {}
        if not cypher:
            raise ValueError("empty cypher")
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        return cypher, params
    except Exception as exc:
        raise RuntimeError(f"Invalid LLM Cypher response: {exc}") from exc


def _validate_read_only(cypher: str, *, max_rows: int) -> str:
    s = cypher.strip().rstrip(";")
    if not s:
        raise RuntimeError("Generated Cypher is empty.")

    upper = s.upper()
    for pat in _FORBIDDEN:
        if re.search(pat, upper, flags=re.IGNORECASE):
            raise RuntimeError(f"Rejected unsafe Cypher containing forbidden clause: {pat}")

    # Soft allowlist by leading clause (most read-only queries start with MATCH/WITH/CALL).
    if not re.match(r"^(MATCH|OPTIONAL\s+MATCH|WITH|CALL)\b", s, flags=re.IGNORECASE):
        raise RuntimeError("Rejected Cypher: query must begin with MATCH/OPTIONAL MATCH/WITH/CALL.")

    # Ensure bounded result size.
    if not _LIMIT_RE.search(s):
        s = f"{s}\nLIMIT {int(max_rows)}"

    return s
