"""LLM layer for snippet-driven PR review.

Two entry points, both copying the stdlib-``urllib`` OpenAI call pattern from
:mod:`cpg.cypher_qa` (no SDK dependency):

- :func:`llm_pick_nodes`  — optional fallback entity extraction: given a snippet
  and a catalog of graph nodes, return the ids the snippet relates to.
- :func:`generate_review` — the actual review: given the snippet under review and
  the graph-connected code bundle, return reviewer feedback.

Environment variables:
- OPENAI_API_KEY (required)
- OPENAI_BASE_URL (optional, default https://api.openai.com/v1)
- OPENAI_MODEL (optional, default gpt-4o-mini)
"""
from __future__ import annotations

import json
import os
import urllib.request

# Load OPENAI_* (and any other) variables from a local .env if python-dotenv is
# installed. Best-effort: absence of the package or file is not an error.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - convenience only
    pass


def _chat(messages: list[dict], *, response_format: dict | None = None,
          temperature: float = 0, timeout: int = 90) -> str:
    """POST a chat completion and return the message content string."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for the LLM review layer.")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    body: dict = {"model": model, "temperature": temperature, "messages": messages}
    if response_format:
        body["response_format"] = response_format

    req = urllib.request.Request(
        url=f"{base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface a single clear error
        raise RuntimeError(f"LLM request failed: {exc}") from exc
    return raw["choices"][0]["message"]["content"]


def llm_pick_nodes(snippet: str, catalog: list[dict]) -> list[str]:
    """Return node ids from *catalog* that *snippet* references (LLM fallback)."""
    lines = "\n".join(
        f'{c["id"]} | {c.get("kind", "")} | {c.get("name", "")} | {c.get("file", "")}'
        for c in catalog
    )
    system = (
        "You map a code snippet to the graph nodes it references. "
        "Output STRICT JSON only: {\"ids\": [string, ...]}. "
        "Choose only ids from the provided catalog; pick the entities the "
        "snippet defines, calls, imports, or routes to. Return [] if none apply."
    )
    user = (
        f"Snippet:\n```\n{snippet}\n```\n\n"
        f"Catalog (id | kind | name | file):\n{lines}\n"
    )
    content = _chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        response_format={"type": "json_object"},
    )
    try:
        obj = json.loads(content)
        ids = obj.get("ids", [])
        return [str(i) for i in ids] if isinstance(ids, list) else []
    except Exception:
        return []


def generate_review(snippet: str, bundle: list[dict], *,
                    char_budget: int = 24000) -> str:
    """Return PR-review feedback for *snippet* given the connected *bundle*.

    *bundle* items are the dicts produced by ``cpg.retrieve.context_for`` /
    the fixed-depth path: ``{id, kind, name, file, source, hops}``.
    """
    blocks: list[str] = []
    used = 0
    for item in bundle:
        header = (f'### {item.get("kind", "")} {item.get("name", "")}\n'
                  f'{item.get("id", "")}  ({item.get("file", "")})')
        block = f'{header}\n```\n{item.get("source", "")}\n```'
        if used + len(block) > char_budget:
            break
        blocks.append(block)
        used += len(block)
    context = "\n\n".join(blocks)

    system = (
        "You are a senior engineer reviewing a pull request. You are given the "
        "code under review plus the related code pulled from a code knowledge "
        "graph (callers, callees, routes, tables, auth dependencies). Review for "
        "correctness, security (authorization and data access), breaking changes, "
        "and impact on the related code. Cite related nodes by name when relevant. "
        "Be concise and specific; if the context is insufficient, say so."
    )
    user = (
        f"## Code under review\n```\n{snippet}\n```\n\n"
        f"## Related code from the graph\n{context}\n"
    )
    return _chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        temperature=0.2,
    )
