"""Shared "always-give-the-LLM-real-context" helpers used by both Agent A
(agents.py) and Agent B (taint.py).

Three kinds of context must reach the LLM whenever a function under review
could plausibly touch them, REGARDLESS of which chunk/pass/call is being
built — never gated behind "only the file's first chunk" or "only if we
happened to read that line range":

  1. class-level fields  (shared_state_for_ids, via real READS/WRITES edges)
  2. module-level globals (also Field nodes — same query as #1, since the
     Python extractor emits module globals as Field(kind="global_variable"))
  3. imported names       (file_imports_block — the file's import statements,
                            re-sent on every call for that file, not just once)

Found live: Agent A's chunker only attached the file's import lines to its
FIRST chunk (`include_preamble=(i == 0)`), so any function reviewed in a
later chunk of a multi-chunk file never saw what was imported. Agent B's
taint-qualify step never sent import/shared-state context at all, only each
chain function's raw body. Both are precision bugs — the LLM can't reason
about whether a name is a stdlib call, a sanitizer from an imported helper,
or a shared/global mutable without this.
"""
from __future__ import annotations

import os
from functools import lru_cache

from ..graph_core.store import GraphStore


def shared_state_for_ids(store: GraphStore, node_ids: list[str]) -> str:
    """Real shared-state facts for a set of Function node ids: which
    class-level or module-level (global) fields they read/write, from actual
    READS/WRITES edges — never name-guessing."""
    if not node_ids:
        return ""
    rows = store.read(
        "MATCH (n:Function) WHERE n.id IN $ids "
        "OPTIONAL MATCH (n)-[:READS]->(rf:Field) "
        "OPTIONAL MATCH (n)-[:WRITES]->(wf:Field) "
        "RETURN collect(DISTINCT rf.name) AS reads_fields, collect(DISTINCT wf.name) AS writes_fields",
        ids=node_ids,
    )
    if not rows:
        return ""
    reads = sorted({r for r in (rows[0].get("reads_fields") or []) if r})
    writes = sorted({w for w in (rows[0].get("writes_fields") or []) if w})
    if not reads and not writes:
        return ""
    parts = []
    if reads:
        parts.append(f"reads shared fields: {', '.join(reads)}")
    if writes:
        parts.append(f"writes shared fields: {', '.join(writes)}")
    return "; ".join(parts)


@lru_cache(maxsize=4096)
def _file_imports_block_cached(abspath: str, mtime: float) -> str:
    try:
        with open(abspath, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    import_lines = [
        ln.rstrip("\n") for ln in lines
        if ln.strip().startswith("import ") or ln.strip().startswith("from ")
    ]
    return "\n".join(import_lines)


def file_imports_block(root: str, file: str) -> str:
    """This file's `import`/`from ... import` lines, re-derived once per file
    (cached by path+mtime) and meant to be re-sent on EVERY chunk/call for
    that file — not just a file's first chunk — so a function reviewed
    anywhere in the file still shows the LLM what names are imported."""
    if not file:
        return ""
    abspath = os.path.join(os.path.abspath(root), file)
    try:
        mtime = os.path.getmtime(abspath)
    except OSError:
        return ""
    return _file_imports_block_cached(abspath, mtime)
