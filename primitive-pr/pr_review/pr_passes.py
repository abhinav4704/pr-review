"""Multi-pass LLM PR review, driven by the Neo4j code graph.

For each file in the diff we run up to three passes:

  1. Whole file          -> breakage, exposed secrets/keys, optimization.
  2. Changed fn + callers + callees -> breaking-change / contract drift.
  3. Changed fn + the functions it calls from OTHER files -> correctness /
     missing-logic, using that imported context.

Each pass asks the model for JSON findings (see findings.FINDINGS_SCHEMA).
Oversized inputs are split into chunks and the findings merged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List

from .findings import (
    FINDINGS_SCHEMA,
    Finding,
    dedupe,
    parse_findings,
    sort_by_severity,
)

# A completion function: fn(system, user) -> str
CompleteFn = Callable[[str, str], str]

MAX_RELATED = 8          # cap callers/callees included per function
DEFAULT_BUDGET = 12000   # char budget per LLM call before chunking


# ── source reading ───────────────────────────────────────────────────────────
def read_source(src_path: str, file_path: str, start_line: int, end_line: int) -> str:
    full = Path(src_path) / file_path
    if not full.exists():
        return ""
    lines = full.read_text(errors="replace").splitlines()
    return "\n".join(lines[max(0, start_line - 1):end_line])


def read_file(src_path: str, file_path: str) -> str:
    full = Path(src_path) / file_path
    if not full.exists():
        return ""
    return full.read_text(errors="replace")


def _chunk(text: str, budget: int) -> List[str]:
    """Split text on line boundaries into <=budget-char chunks."""
    if len(text) <= budget:
        return [text]
    chunks: List[str] = []
    cur: List[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > budget and cur:
            chunks.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        chunks.append("".join(cur))
    return chunks


# ── system prompts ───────────────────────────────────────────────────────────
WHOLE_FILE_SYSTEM = (
    "You are reviewing a PULL REQUEST. You are given the unified DIFF for one file (what "
    "the PR changed) and the FULL FILE as context. Review ONLY what the diff changed: "
    "report things the change breaks, bugs it introduces, vulnerabilities (an exposed "
    "secret/key/credential is critical; an unaddressed weakness like SQL injection / "
    "unsanitized input / missing auth is high or medium), unoptimized code, and optional "
    "suggestions. Use the full file to judge (e.g. to confirm a vulnerability) but do not "
    "flag pre-existing issues unrelated to the change.\n\n" + FINDINGS_SCHEMA
)

BREAKING_SYSTEM = (
    "You are reviewing a PULL REQUEST. You are given a changed function with the CHANGED "
    "lines marked '>>>', the functions that CALL it (callers), and the functions it CALLS "
    "(callees). Decide whether the marked change BREAKS its callers (a changed return "
    "value/type, parameters, exceptions or side effects that would make a caller fail = "
    "'breaking', critical) or MISUSES its callees ('bug'). Only report problems caused by "
    "the marked change.\n\n" + FINDINGS_SCHEMA
)

CORRECTNESS_SYSTEM = (
    "You are reviewing a PULL REQUEST. You are given a changed function with the CHANGED "
    "lines marked '>>>', plus the full source of the functions it calls from OTHER files "
    "(its imported/used dependencies). Check that the marked change uses those functions "
    "correctly (right arguments, handles their return values and errors) and misses no "
    "necessary step or error handling. Report such problems as 'bug'. Only report problems "
    "caused by the marked change.\n\n" + FINDINGS_SCHEMA
)


def annotate_changed(src: str, start_line: int, added_lines) -> str:
    """Prefix lines whose new-side number is in added_lines with '>>>' markers."""
    if not added_lines:
        return src
    out = []
    for offset, line in enumerate(src.splitlines()):
        lineno = start_line + offset
        out.append(f">>> {line}" if lineno in added_lines else f"    {line}")
    return "\n".join(out)


def _node_name(n: dict) -> str:
    return n.get("qualname") or n.get("name") or n.get("id") or "?"


# ── pass 1: whole file ───────────────────────────────────────────────────────
def pass_whole_file(file_path: str, src_path: str, complete: CompleteFn,
                    budget: int = DEFAULT_BUDGET, diff_text: str = "") -> List[Finding]:
    source = read_file(src_path, file_path)
    if not source.strip():
        return []
    diff_block = f"## Diff (what changed)\n```diff\n{diff_text}\n```\n\n" if diff_text else ""
    findings: List[Finding] = []
    chunks = _chunk(source, budget)
    for i, chunk in enumerate(chunks):
        part = f" (part {i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        user = (f"# File: {file_path}{part}\n\n{diff_block}"
                f"## Full file (context)\n```\n{chunk}\n```")
        findings += parse_findings(complete(WHOLE_FILE_SYSTEM, user), default_file=file_path)
    return findings


# ── pass 2: breaking-change (callers + callees) ──────────────────────────────
def pass_breaking(store, src_path: str, pr_ref: str, node: dict,
                  complete: CompleteFn, budget: int = DEFAULT_BUDGET,
                  include_dependees: bool = False, added_lines=None) -> List[Finding]:
    file_path = node["path"]
    changed_src = read_source(src_path, file_path, node["start_line"], node["end_line"])
    if not changed_src.strip():
        return []
    changed_src = annotate_changed(changed_src, node["start_line"], added_lines)

    # Dependents (callers) are always checked; dependees (callees) only on request.
    callers = store.neighbors(node["id"], "CALLS", "in")[:MAX_RELATED]
    callees = store.neighbors(node["id"], "CALLS", "out")[:MAX_RELATED] if include_dependees else []

    parts = [
        f"# Changed {node['kind']}: {_node_name(node)} ({file_path})  "
        f"(lines marked >>> are the change)\n",
        f"```\n{changed_src}\n```\n",
    ]
    if callers:
        parts.append("## Callers (depend on this function)\n")
        for c in callers:
            src = read_source(src_path, c["path"], c.get("start_line", 0), c.get("end_line", 0))
            parts.append(f"### {_node_name(c)} ({c['path']})\n```\n{src}\n```\n")
    if callees:
        parts.append("## Callees (functions this calls)\n")
        for c in callees:
            src = read_source(src_path, c["path"], c.get("start_line", 0), c.get("end_line", 0))
            parts.append(f"### {_node_name(c)} ({c['path']})\n```\n{src}\n```\n")

    return _run_chunked(BREAKING_SYSTEM, "".join(parts), file_path, complete, budget)


# ── pass 3: correctness (imported/used functions) ────────────────────────────
def pass_correctness(store, src_path: str, pr_ref: str, node: dict,
                     complete: CompleteFn, budget: int = DEFAULT_BUDGET,
                     added_lines=None) -> List[Finding]:
    file_path = node["path"]
    changed_src = read_source(src_path, file_path, node["start_line"], node["end_line"])
    if not changed_src.strip():
        return []

    # functions it calls that live in OTHER files = its imported/used dependencies
    callees = store.neighbors(node["id"], "CALLS", "out")
    imported = [c for c in callees if c.get("path") and c["path"] != file_path][:MAX_RELATED]
    if not imported:
        return []

    changed_src = annotate_changed(changed_src, node["start_line"], added_lines)
    parts = [
        f"# Changed {node['kind']}: {_node_name(node)} ({file_path})  "
        f"(lines marked >>> are the change)\n",
        f"```\n{changed_src}\n```\n",
        "## Imported/used functions (from other files)\n",
    ]
    for c in imported:
        src = read_source(src_path, c["path"], c.get("start_line", 0), c.get("end_line", 0))
        parts.append(f"### {_node_name(c)} ({c['path']})\n```\n{src}\n```\n")

    return _run_chunked(CORRECTNESS_SYSTEM, "".join(parts), file_path, complete, budget)


def _run_chunked(system: str, user: str, file_path: str,
                 complete: CompleteFn, budget: int) -> List[Finding]:
    findings: List[Finding] = []
    for chunk in _chunk(user, budget):
        findings += parse_findings(complete(system, chunk), default_file=file_path)
    return findings


# ── per-file orchestration ───────────────────────────────────────────────────
def review_file(store, src_path: str, pr_ref: str, file_diff, complete: CompleteFn,
                budget: int = DEFAULT_BUDGET, include_dependees: bool = False,
                diff_text: str = "") -> List[Finding]:
    file_path = file_diff.path
    added_lines = file_diff.added_lines
    findings: List[Finding] = []

    # pass 1 — whole file (diff + full file as context)
    findings += pass_whole_file(file_path, src_path, complete, budget, diff_text=diff_text)

    # passes 2 + 3 — per changed node (changed lines marked)
    changed_nodes = store.nodes_at_lines(
        file_path, sorted(added_lines), pr_ref
    ) if added_lines else []
    for node in changed_nodes:
        # pass 2 always runs (dependents/callers); pass 3 is the dependee check.
        findings += pass_breaking(store, src_path, pr_ref, node, complete, budget,
                                  include_dependees=include_dependees, added_lines=added_lines)
        if include_dependees:
            findings += pass_correctness(store, src_path, pr_ref, node, complete, budget,
                                         added_lines=added_lines)

    return sort_by_severity(dedupe(findings))


def review_pr(store, src_path: str, pr_ref: str, file_diffs, complete: CompleteFn,
              budget: int = DEFAULT_BUDGET, progress_cb=None,
              include_dependees: bool = False,
              diff_by_file: Dict[str, str] = None) -> Dict[str, List[Finding]]:
    diff_by_file = diff_by_file or {}
    results: Dict[str, List[Finding]] = {}
    total = len(file_diffs)
    for i, fd in enumerate(file_diffs):
        if progress_cb:
            progress_cb(i, total, fd.path)
        results[fd.path] = review_file(store, src_path, pr_ref, fd, complete, budget,
                                       include_dependees=include_dependees,
                                       diff_text=diff_by_file.get(fd.path, ""))
    if progress_cb:
        progress_cb(total, total, "")
    return results
