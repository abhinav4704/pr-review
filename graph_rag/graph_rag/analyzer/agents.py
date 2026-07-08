"""Agent A — merged correctness + impact, one call per chunk of whole functions.

Old design had 2 separate agents (correctness=self+raw callees, impact=self+
caller shapes) as 2 separate LLM calls per function. Merged per the new
2-agent plan: Agent A now asks ONE question per chunk of whole functions from
a file — "is this code wrong on its own terms, AND if it's wrong (or its
contract changes), who's affected by the callers we can see?" — using:
  - the chunk's own raw source (never split mid-function)
  - real shared-state facts (module/class fields read/written, from the graph,
    not name-guessing)
  - Layer-3 taint facts (self reaches a known sink) when present
  - each function's known callers, shown only as *shape* (signature,
    docstring, role) — never raw caller bodies, since impact only needs to
    know whether a caller's shape indicates genuinely dangerous/fragile usage

This is Agent A out of the new 2-agent design (Agent B = taint + architecture,
in taint.py). No identity/flow generation is used here — everything is read
directly from source + the structural graph.

God-node guard: caller lookups are capped at `_FANOUT_GUARD` per function so a
god-function's caller list doesn't blow up chunk size.
"""
from __future__ import annotations

import os

from ..graph_core.findings import Finding
from ..graph_core.store import GraphStore
from .context import file_imports_block, shared_state_for_ids

_FANOUT_GUARD = 40

# Canonical subcategory vocabulary (matches findings.SEVERITY_BASE so scoring's
# severity formula has an actual base to look up, instead of silently falling
# back to DEFAULT_SEVERITY_BASE for free-text subcategories). Found live:
# without this, agents invented subcategories like a full sentence, or
# duplicated the category into the subcategory ("impact/breakage") — both
# defeat the severity lookup.
_CORRECTNESS_SUBCATS = [
    "null_deref", "unhandled_empty", "bad_error_handling", "resource_double_release",
    "race_condition", "off_by_one", "sql_injection", "command_injection",
]
_IMPACT_SUBCATS = ["breakage", "resource_management", "contract_change"]

# Additional subcategories originally from a standalone single-file pass,
# folded into Agent A's chunk taxonomy.
_SINGLE_FILE_SUBCATS = [
    "incorrect_comparison", "mutable_default_argument", "incorrect_boolean_logic",
    "type_confusion", "unreachable_code", "bare_except", "swallowed_exception",
    "silent_data_loss", "hardcoded_secret", "weak_crypto", "insecure_randomness",
    "insecure_config", "redos_vulnerable_regex", "sensitive_data_logged",
    "resource_leak", "missing_timeout", "unbounded_retry", "n_plus_one_query",
    "blocking_call_in_loop", "quadratic_algorithm", "inefficient_string_concat",
    "redundant_recomputation", "unnecessary_full_materialization",
    "repeated_compile_in_loop",
]

_AGENT_A_SUBCATS = sorted(set(_CORRECTNESS_SUBCATS) | set(_SINGLE_FILE_SUBCATS) | set(_IMPACT_SUBCATS))


def _read_source(root: str, file: str, start_line: int, end_line: int) -> str:
    if not file:
        return ""
    abspath = os.path.join(os.path.abspath(root), file)
    try:
        with open(abspath, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    start = max((start_line or 1) - 1, 0)
    end = end_line or (start + 1)
    return "".join(lines[start:end])


def select_files(store: GraphStore, repo: str, limit: int | None = None) -> list[str]:
    """Distinct file paths that have at least one indexed Function."""
    rows = store.read(
        "MATCH (n:Function {repo:$repo}) WHERE n.file IS NOT NULL "
        "RETURN DISTINCT n.file AS file ORDER BY file",
        repo=repo,
    )
    files = [r["file"] for r in rows if r.get("file")]
    return files[:limit] if limit else files


def _function_spans(store: GraphStore, repo: str, file: str) -> list[dict]:
    rows = store.read(
        "MATCH (n:Function {repo:$repo, file:$file}) "
        "RETURN n.id AS id, n.fqn AS fqn, n.name AS name, "
        "n.start_line AS start_line, n.end_line AS end_line, "
        "n.taint_sink_count AS taint_sink_count",
        repo=repo, file=file,
    )
    return [r for r in rows if r.get("start_line")]


def _build_chunks(spans: list[dict], max_lines: int = 150) -> list[list[dict]]:
    """Group consecutive whole functions so each chunk's line span stays under
    `max_lines` — never splits a function (a single function longer than
    `max_lines` still just gets its own, larger, chunk)."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_start = None
    for sp in spans:
        span_end = sp["end_line"] or sp["start_line"]
        if not current:
            current = [sp]
            current_start = sp["start_line"]
            continue
        if (span_end - current_start + 1) > max_lines:
            chunks.append(current)
            current = [sp]
            current_start = sp["start_line"]
        else:
            current.append(sp)
    if current:
        chunks.append(current)
    return chunks


def _callers(store: GraphStore, node_id: str) -> list[dict]:
    rows = store.read(
        "MATCH (n:Function)-[:CALLS]->(m:Function {id:$id}) "
        "RETURN DISTINCT n.id AS id, n.fqn AS fqn, n.name AS name, n.file AS file, "
        "n.signature AS signature, n.docstring AS docstring, n.component_role AS component_role, "
        "n.role_confidence AS role_confidence "
        "LIMIT $cap",
        id=node_id, cap=_FANOUT_GUARD,
    )
    return rows


def _hop2_summary(store: GraphStore, caller_id: str) -> str:
    """Grandcallers (hop 2) of a single hop-1 caller, combined into a cheap
    role-grouped count summary — never individual detail — so the prompt gets
    a signal for how much wider the blast radius goes beyond the immediate
    caller without paying per-grandcaller token/LLM cost."""
    rows = store.read(
        "MATCH (gc:Function)-[:CALLS]->(:Function {id:$id}) "
        "RETURN DISTINCT gc.id AS id, gc.component_role AS component_role "
        "LIMIT $cap",
        id=caller_id, cap=_FANOUT_GUARD,
    )
    if not rows:
        return "none known"
    counts: dict[str, int] = {}
    for r in rows:
        role = r.get("component_role") or "unknown"
        counts[role] = counts.get(role, 0) + 1
    return ", ".join(f"{role}={n}" for role, n in sorted(counts.items()))


def _caller_block(store: GraphStore, fqn: str, callers: list[dict]) -> str:
    if not callers:
        return f"--- callers of {fqn}: none known ---"
    lines = [f"--- callers of {fqn} ---"]
    for c in callers:
        lines.append(
            f"  caller {c['fqn']} (role={c.get('component_role') or 'unknown'}, "
            f"role_conf={c.get('role_confidence') or '?'}): "
            f"signature={c.get('signature') or ''} docstring={c.get('docstring') or ''}"
        )
        lines.append(f"    hop-2 callers of {c['fqn']} (combined, by role): {_hop2_summary(store, c['id'])}")
    return "\n".join(lines)


_AGENT_A_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "function": {"type": "string"},
                    "subcategory": {"type": "string", "enum": _AGENT_A_SUBCATS},
                    "line": {"type": "integer"},
                    "message": {"type": "string"},
                    "evidence": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                },
                "required": ["function", "subcategory", "line", "message", "evidence", "confidence"],
            },
        }
    },
    "required": ["findings"],
}

_AGENT_A_SYSTEM = (
    "You are reviewing a GROUP of one or more whole functions from the same file "
    "(shown together to save cost — never split mid-function) for TWO things at once: "
    "(A) correctness/security/performance bugs IN these functions' own bodies, and "
    "(B) impact/breakage risk — given each function's KNOWN CALLERS shown only as shape "
    "(signature, docstring, role, role-confidence — never the caller's raw body), would a "
    "change to this function's contract (return shape, exceptions, resource ownership, "
    "argument expectations) break a caller, or is a resource this function returns/holds "
    "released exactly once across that caller boundary (not zero, not twice)?\n\n"
    "Caller context has two levels: each hop-1 caller (direct caller) is shown with its "
    "full shape (signature, docstring, role); nested under it, its OWN callers (hop 2 — "
    "the direct caller's callers) are shown only as a combined role-count summary, not "
    "individual detail. Use hop-2 counts only as a coarse blast-radius signal (a hop-1 "
    "caller with a large/varied hop-2 fan-out is a higher-impact caller to break) — never "
    "cite a hop-2 entry as if you saw its actual code, since you didn't.\n\n"
    "For EVERY finding, set \"function\" to the exact name of the function it belongs to as "
    "shown in the chunk (or \"\" if it's truly module-level code). Only report something from "
    "this exact list of subcategories — never invent a new one, and never relabel a finding "
    "under an allowed category if it's actually a different (forbidden) kind of bug:\n\n"
    "CORRECTNESS: null_deref, off_by_one, incorrect_comparison, mutable_default_argument, "
    "incorrect_boolean_logic, type_confusion, unreachable_code, unhandled_empty (this function "
    "ITSELF indexes/dereferences into a collection or lookup result — e.g. result[0] — without "
    "checking length/None first; simply forwarding a return value unchanged to your own caller "
    "is NOT unhandled_empty, since nothing was actually indexed into here), bad_error_handling "
    "(an actual try/except (or equivalent) block IS present and it swallows, discards, or "
    "mishandles the real error — do NOT use this just because a function has no error handling "
    "at all; the mere absence of a try/except around a call is not itself a finding unless you "
    "can point to a SPECIFIC visible failure mode, e.g. a resource left open or a caller shown "
    "here that can't handle the exception propagating), race_condition "
    "(read-modify-write on shared module/class state with no lock between the read and the "
    "write — only flag this if the field is confirmed shared state per the facts you're given, "
    "not from naming alone).\n\n"
    "TAINT-CLASS (only when the untrusted input AND the sink are both visible in the code you "
    "can see right here — do not guess about validation elsewhere): sql_injection (untrusted "
    "input reaches a SQL string/query — e.g. a DB cursor/connection .execute()/.query() call, "
    "an ORM raw() call, or a SQL string built via f-string/format/concatenation — never use "
    "command_injection for this, even if the vulnerable string is called \"cmd\" or \"sql\" as a "
    "variable name), command_injection (untrusted input reaches an actual OS shell/process "
    "call — os.system, subprocess.*, os.popen, shell=True — do NOT use this for SQL sinks, "
    "regardless of how the query string was built).\n\n"
    "ERROR HANDLING: bare_except, swallowed_exception, silent_data_loss.\n\n"
    "SECURITY (non-taint, provable from this code alone): hardcoded_secret (a literal that is "
    "plausibly a real API key/password/token, not a config key NAME or a placeholder like "
    "\"TODO\"/\"xxx\"), weak_crypto, insecure_randomness, insecure_config, "
    "redos_vulnerable_regex, sensitive_data_logged.\n\n"
    "RESOURCE/RELIABILITY (only if the risky open/close or retry is fully visible in what you "
    "see): resource_leak, resource_double_release, missing_timeout, unbounded_retry.\n\n"
    "PERFORMANCE (\"unoptimizations\"): n_plus_one_query, blocking_call_in_loop, "
    "quadratic_algorithm, inefficient_string_concat, redundant_recomputation, "
    "unnecessary_full_materialization, repeated_compile_in_loop.\n\n"
    "IMPACT/BREAKAGE (uses the caller-shape blocks given below, not raw caller code): breakage "
    "(a caller's shown shape indicates it would break if this function's behavior/contract "
    "changed), resource_management (a resource this function owns/returns is not clearly "
    "released exactly once across the caller boundary), contract_change (changing this "
    "function's signature/return shape would visibly break a shown caller).\n\n"
    "EXPLICITLY DO NOT REPORT, under any label (these require tracing input origin across "
    "functions/files you cannot see here, and restating them under an allowed category is "
    "still wrong): eval_injection, template_injection, xss, path_traversal, ssrf, "
    "deserialization, mass_assignment, missing_authorization.\n\n"
    "RULES:\n"
    "- Every finding needs: function, subcategory (from the exact list above), line, message, "
    "and evidence quoting the actual code.\n"
    "- Set confidence: HIGH only when unambiguous from what you see; LOW when you're inferring "
    "intent rather than certain.\n"
    "- Do not report the same underlying issue more than once, and do not report a caller-shape "
    "impact finding unless a caller is actually shown for that function.\n"
    "- Do not flag test/fixture files or obvious placeholder values as hardcoded_secret.\n"
    "- If nothing from the list above is present, return an EMPTY findings array."
)


def run_agent_a_chunk(store: GraphStore, root: str, repo: str, file: str,
                      chunk: list[dict], llm, include_preamble: bool = False) -> list[Finding]:
    """One merged LLM call for a group of whole functions from `file`: self
    source + shared-state facts + taint flags + each function's caller shapes.

    `include_preamble=True` reads from line 1 instead of the chunk's first
    function — used for a file's first chunk only, so module-level constants
    (imports, globals, top-of-file config) are visible at least once without
    resending them on every chunk of the same file.
    """
    start = 1 if include_preamble else chunk[0]["start_line"]
    end = max((sp["end_line"] or sp["start_line"]) for sp in chunk)
    src = _read_source(root, file, start, end)
    if not src.strip():
        return []
    ids = [sp["id"] for sp in chunk]
    shared_state = shared_state_for_ids(store, ids)
    # Always resent for EVERY chunk of a file, not just the first — a later
    # chunk's function can reference an imported name whose `import`/`from`
    # line lives before this chunk's own read window.
    imports_block = file_imports_block(root, file)
    fn_names = ", ".join(sp["name"] for sp in chunk if sp.get("name"))

    taint_flags = [
        f"- {sp['fqn']} reaches a known dangerous sink per Layer-3 taint facts "
        "(judge whether the value can actually be attacker-controlled from its callers)."
        for sp in chunk if (sp.get("taint_sink_count") or 0) > 0
    ]
    caller_blocks = [
        _caller_block(store, sp["fqn"], _callers(store, sp["id"]))
        for sp in chunk if sp.get("id")
    ]

    user = (
        f"file: {file}\n"
        f"functions in this chunk (lines {start}-{end}): {fn_names}\n\n"
        + (f"--- imports in {file} ---\n{imports_block}\n\n" if imports_block else "")
        + f"--- source ---\n{src}\n\n"
        + (f"{shared_state} (this is real shared state — module/class-level, not a "
           "local variable)\n\n" if shared_state else "")
        + ("targeted checks:\n" + "\n".join(taint_flags) + "\n\n" if taint_flags else "")
        + "\n".join(caller_blocks) + "\n"
    )
    result = llm.extract(_AGENT_A_SYSTEM, user, _AGENT_A_SCHEMA)
    items = result.get("findings", []) if isinstance(result, dict) else []
    by_name = {sp["name"]: sp["fqn"] for sp in chunk if sp.get("name")}
    out = []
    for item in items:
        owning_fqn = by_name.get(item.get("function") or "", file)
        category = "impact" if item.get("subcategory") in _IMPACT_SUBCATS else "correctness"
        f = Finding.from_dict(item, category=category, source="llm_judged",
                              owning_fqn=owning_fqn, file=file)
        if f:
            out.append(f)
    return out


def run_agent_a_scan(store: GraphStore, root: str, repo: str, llm,
                     max_lines: int = 150, file_limit: int | None = None,
                     log=None) -> list[Finding]:
    """Agent A over the whole repo: one LLM call per group of whole functions
    per file, covering correctness + impact/breakage in a single pass.

    A single chunk failing (e.g. a transient Bedrock ModelErrorException that
    survives `llm.extract`'s own retries) is logged and skipped rather than
    aborting the whole scan — one bad chunk shouldn't cost every other file's
    findings."""
    if llm is None:
        return []
    findings: list[Finding] = []
    for file in select_files(store, repo, limit=file_limit):
        spans = _function_spans(store, repo, file)
        if not spans:
            continue
        for i, chunk in enumerate(_build_chunks(spans, max_lines=max_lines)):
            try:
                findings.extend(
                    run_agent_a_chunk(store, root, repo, file, chunk, llm, include_preamble=(i == 0))
                )
            except Exception as exc:  # noqa: BLE001 - one bad chunk must not abort the scan
                msg = f"  [agent_a] skipped chunk {i} of {file}: {exc}"
                if log:
                    log(msg)
                else:
                    print(msg)
    return findings
