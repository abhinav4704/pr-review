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

Phase 4 changes:
  - _batch_callers / _batch_hop2_summaries: 2 queries per file instead of
    O(functions) per file — the main performance win.
  - run_agent_a_scan: uses ThreadPoolExecutor(max_workers=6) to process files
    in parallel; order is preserved in the output list.
  - _function_spans: reads dfg_json instead of taint_sink_count so sink-flag
    detection comes from the graph DFG (no separate taint pass needed).

God-node guard: caller lookups are capped at `_FANOUT_GUARD` per function so a
god-function's caller list doesn't blow up chunk size.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..graph_core.findings import Finding
from ..graph_core.store import GraphStore
from .context import file_imports_block, shared_state_for_ids

_FANOUT_GUARD = 40
# How many hop-1 callers get their FULL BODY inlined into a chunk (ranked by
# hop-2 blast relevance). Beyond this, callers appear as signature only — keeps
# a god-function's caller bodies from blowing up the chunk while still giving the
# reviewer real caller code for the highest-impact callers.
_CALLER_BODY_CAP = 4
# Max source lines of any single inlined caller body (a huge caller is truncated
# rather than dominating the chunk).
_CALLER_BODY_MAX_LINES = 60

# Agent A's classification is FREE-FORM (no enforced enum) — the LLM names each
# finding's subcategory precisely and assigns its own base severity. These lists
# remain only as (a) illustrative examples surfaced in the prompt and (b) the
# `_IMPACT_SUBCATS` hint used to bucket a finding's top-level category when the
# LLM's own `category` field is missing/unrecognized. They are NOT a whitelist.
# SEVERITY_BASE in findings.py still covers these known names for the
# deterministic graph_proven path; free-form names lean on the LLM's severity.
_IMPACT_SUBCATS = ["breakage", "resource_management", "contract_change"]


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
        "n.dfg_json AS dfg_json",
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


def _batch_callers(store: GraphStore, ids: list[str]) -> dict[str, list[dict]]:
    """Fetch hop-1 callers for all function ids in one query.
    Returns {target_id: [caller_row, ...]}."""
    if not ids:
        return {}
    rows = store.read(
        "MATCH (c:Function)-[:CALLS]->(m:Function) WHERE m.id IN $ids "
        "RETURN m.id AS target, collect({"
        "id: c.id, fqn: c.fqn, name: c.name, file: c.file, "
        "start_line: c.start_line, end_line: c.end_line, "
        "signature: c.signature, docstring: c.docstring, "
        "component_role: c.component_role, "
        "role_confidence: c.role_confidence"
        "}) AS callers",
        ids=ids,
    )
    return {r["target"]: r["callers"][:_FANOUT_GUARD] for r in rows}


def _batch_hop2_summaries(store: GraphStore, caller_ids: list[str]) -> tuple[dict[str, str], dict[str, int]]:
    """Fetch hop-2 caller role counts for all caller_ids in one query.
    Returns ({caller_id: "role=N, ..."}, {caller_id: total_hop2_count}). The
    totals rank which callers matter most (bigger blast radius) when deciding
    whose body to inline into a chunk."""
    if not caller_ids:
        return {}, {}
    rows = store.read(
        "MATCH (gc:Function)-[:CALLS]->(c:Function) WHERE c.id IN $ids "
        "RETURN c.id AS cid, gc.component_role AS role, count(*) AS cnt",
        ids=caller_ids,
    )
    grouped: dict[str, dict[str, int]] = {}
    for r in rows:
        cid = r["cid"]
        role = r.get("role") or "unknown"
        d = grouped.setdefault(cid, {})
        d[role] = d.get(role, 0) + (r.get("cnt") or 0)
    out: dict[str, str] = {}
    totals: dict[str, int] = {}
    for cid in caller_ids:
        counts = grouped.get(cid)
        if not counts:
            out[cid] = "none known"
            totals[cid] = 0
        else:
            out[cid] = ", ".join(f"{role}={n}" for role, n in sorted(counts.items()))
            totals[cid] = sum(counts.values())
    return out, totals


def _caller_block(fqn: str, callers: list[dict], hop2_map: dict[str, str],
                  root: str = "", hop2_totals: dict[str, int] | None = None) -> str:
    """Render a function's callers. The top `_CALLER_BODY_CAP` callers (ranked by
    hop-2 blast relevance) get their REAL SOURCE inlined so the reviewer can trace
    a value/assumption across the caller boundary; the rest show as signature only
    to bound chunk size. hop-2 stays a role-count summary in both cases."""
    if not callers:
        return f"--- callers of {fqn}: none known ---"
    hop2_totals = hop2_totals or {}
    # Rank by hop-2 total desc so the highest-impact callers get their bodies.
    ranked = sorted(callers, key=lambda c: hop2_totals.get(c.get("id") or "", 0), reverse=True)
    body_ids = {c.get("id") for c in ranked[:_CALLER_BODY_CAP]}

    lines = [f"--- callers of {fqn} ---"]
    for c in ranked:
        cid = c.get("id") or ""
        header = (
            f"  caller {c['fqn']} (role={c.get('component_role') or 'unknown'}, "
            f"role_conf={c.get('role_confidence') or '?'})"
        )
        hop2 = hop2_map.get(cid, "none known")
        if cid in body_ids and root and c.get("file") and c.get("start_line"):
            body = _read_source(root, c["file"], c["start_line"], c.get("end_line") or c["start_line"])
            body = _truncate_lines(body, _CALLER_BODY_MAX_LINES)
            lines.append(header + " — REAL SOURCE:")
            lines.append(body)
            lines.append(f"    hop-2 callers of {c['fqn']} (combined, by role): {hop2}")
        else:
            lines.append(
                header + ": "
                f"signature={c.get('signature') or ''} docstring={c.get('docstring') or ''}"
            )
            lines.append(f"    hop-2 callers of {c['fqn']} (combined, by role): {hop2}")
    return "\n".join(lines)


def _truncate_lines(src: str, max_lines: int) -> str:
    """Cap a source block to max_lines, appending an elision marker if cut."""
    parts = src.splitlines()
    if len(parts) <= max_lines:
        return src
    return "\n".join(parts[:max_lines]) + f"\n    ... ({len(parts) - max_lines} more lines elided)"


def _has_sink_flows(dfg_json_str: str | None, catalog) -> bool:
    """True if the function has at least one ArgFlow into a known sink with a
    param flowing in. Uses the SinkCatalog from sinks.py."""
    if not dfg_json_str:
        return False
    try:
        data = json.loads(dfg_json_str)
    except (TypeError, ValueError):
        return False
    for af in data.get("passes", []):
        if af.get("from_params") and catalog.classify(af.get("recv", "") or "", af.get("callee", "") or ""):
            return True
    return False


_AGENT_A_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "function": {"type": "string"},
                    "category": {
                        "type": "string",
                        "description": "one of: correctness | security | performance | impact | design",
                    },
                    "subcategory": {
                        "type": "string",
                        "description": (
                            "a concise snake_case name for the specific issue, e.g. "
                            "null_deref, off_by_one, n_plus_one_query, breakage. Pick the "
                            "most precise name — do NOT force it into a preset list."
                        ),
                    },
                    "line": {"type": "integer"},
                    "message": {"type": "string"},
                    "evidence": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "severity": {
                        "type": "number",
                        "description": "how bad IF real, 0-10 (ignore reachability — scoring adds that)",
                    },
                    "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                },
                "required": ["function", "category", "subcategory", "line", "message", "evidence", "severity", "confidence"],
            },
        }
    },
    "required": ["findings"],
}

_AGENT_A_SYSTEM = (
    "You are a senior engineer reviewing a GROUP of one or more whole functions from the "
    "same file (shown together to save cost — never split mid-function) for real defects "
    "you can PROVE from the code in front of you. Two angles at once:\n"
    "(A) CORRECTNESS / RELIABILITY / PERFORMANCE bugs in these functions' own bodies, and\n"
    "(B) IMPACT / BREAKAGE — using each function's KNOWN CALLERS' actual code shown below, "
    "would changing this function's contract (return shape, exceptions, resource ownership, "
    "argument expectations) break a caller? Is a resource it returns/holds released exactly "
    "once across the caller boundary (not zero, not twice)? Because you can now see the "
    "caller bodies, you may trace a value or an assumption from a caller into this function "
    "and back — do that when it reveals a concrete bug.\n\n"
    "Caller context has two levels: each hop-1 caller (direct caller) is shown with its "
    "REAL SOURCE (up to a cap; beyond it, remaining callers show as signature only). Nested "
    "under each is a combined role-count summary of its OWN callers (hop 2) — a coarse "
    "blast-radius signal only; never cite a hop-2 entry as if you saw its code.\n\n"
    "CLASSIFICATION IS FREE-FORM. Name each finding with the most precise concise snake_case "
    "subcategory you can — do NOT force it into a preset list, and do NOT water a specific bug "
    "down into a vaguer label. Set `category` to one of correctness | security | performance | "
    "impact | design. Common examples (illustrative, NOT exhaustive — invent a better name "
    "when warranted):\n"
    "  correctness: null_deref, off_by_one, incorrect_comparison, mutable_default_argument, "
    "incorrect_boolean_logic, type_confusion, unreachable_code, unhandled_empty, race_condition\n"
    "  reliability: bad_error_handling, bare_except, swallowed_exception, silent_data_loss, "
    "resource_leak, resource_double_release, missing_timeout, unbounded_retry\n"
    "  performance: n_plus_one_query, blocking_call_in_loop, quadratic_algorithm, "
    "inefficient_string_concat, redundant_recomputation, unnecessary_full_materialization\n"
    "  security (only when BOTH the untrusted input and the dangerous use are visible here — "
    "do not speculate about origins you can't see): sql_injection, command_injection, "
    "hardcoded_secret, weak_crypto, insecure_randomness, sensitive_data_logged, etc.\n"
    "  impact/breakage: breakage, contract_change, resource_management\n\n"
    "Deep source-to-sink taint tracing across many files is a DIFFERENT reviewer's job — you "
    "don't need to chase input origin through code you can't see. But if a security bug is "
    "fully provable from what's in front of you, report it; don't suppress it just because it "
    "looks security-ish.\n\n"
    "SEVERITY: for each finding set `severity` 0-10 = how bad IF real, judged from the bug "
    "class alone (a SQLi ~9, a hardcoded secret ~8, an inefficient concat ~2). Ignore "
    "reachability/blast — the scorer multiplies that in afterward.\n\n"
    "RULES:\n"
    "- Set `function` to the exact function name from the chunk (or \"\" for module-level code).\n"
    "- Every finding needs: function, category, subcategory, line, message, evidence quoting "
    "the actual code, and severity.\n"
    "- confidence: HIGH only when unambiguous from what you see; LOW when inferring intent.\n"
    "- Report each underlying issue once; don't raise a caller-impact finding unless a caller "
    "is actually shown for that function.\n"
    "- Don't flag obvious placeholder values (\"TODO\"/\"xxx\") or test fixtures as real secrets.\n"
    "- If there is no real, provable defect, return an EMPTY findings array. Do not invent "
    "findings to look thorough."
)


def run_agent_a_chunk(store: GraphStore, root: str, repo: str, file: str,
                      chunk: list[dict], llm, include_preamble: bool = False,
                      catalog=None, _callers_map: dict | None = None,
                      _hop2_map: dict | None = None,
                      _hop2_totals: dict | None = None) -> list[Finding]:
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

    callers_map = _callers_map or {}
    hop2_map = _hop2_map or {}
    hop2_totals = _hop2_totals or {}

    taint_flags = [
        f"- {sp['fqn']} reaches a known dangerous sink per graph DFG taint facts "
        "(judge whether the value can actually be attacker-controlled from its callers)."
        for sp in chunk
        if catalog is not None and _has_sink_flows(sp.get("dfg_json"), catalog)
    ]
    caller_blocks = [
        _caller_block(sp["fqn"], callers_map.get(sp.get("id") or "", []), hop2_map,
                      root=root, hop2_totals=hop2_totals)
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
        category = _normalize_category(item.get("category"), item.get("subcategory"))
        f = Finding.from_dict(item, category=category, source="llm_judged",
                              owning_fqn=owning_fqn, file=file)
        if f:
            out.append(f)
    return out


_KNOWN_CATEGORIES = {"correctness", "security", "performance", "impact", "design"}


def _normalize_category(raw, subcategory) -> str:
    """Map the LLM's free-form category to a known bucket, with a sane fallback.
    We don't force the subcategory into an enum, but the top-level category is a
    small closed set the rest of the pipeline (scoring, dedup) groups on."""
    cat = str(raw or "").strip().lower()
    if cat in _KNOWN_CATEGORIES:
        return cat
    # Fallback for older/looser outputs: infer from the legacy impact vocabulary.
    if subcategory in _IMPACT_SUBCATS:
        return "impact"
    return "correctness"


def run_agent_a_scan(store: GraphStore, root: str, repo: str, llm,
                     max_lines: int = 150, file_limit: int | None = None,
                     log=None) -> list[Finding]:
    """Agent A over the whole repo: one LLM call per group of whole functions
    per file, covering correctness + impact/breakage in a single pass.

    Phase 4: 2 batched graph queries per file (callers + hop-2 summaries)
    replace O(functions) queries; files are processed with up to 6 parallel
    workers. Output order matches the input file order.

    A single chunk failing (e.g. a transient Bedrock ModelErrorException that
    survives `llm.extract`'s own retries) is logged and skipped rather than
    aborting the whole scan — one bad chunk shouldn't cost every other file's
    findings."""
    if llm is None:
        return []

    from .sinks import load_sinks
    catalog = load_sinks(root)
    files = select_files(store, repo, limit=file_limit)

    def _process_file(file: str) -> list[Finding]:
        spans = _function_spans(store, repo, file)
        if not spans:
            return []
        ids = [sp["id"] for sp in spans if sp.get("id")]
        callers_map = _batch_callers(store, ids)
        all_caller_ids = [
            c.get("id") for callers in callers_map.values() for c in callers if c.get("id")
        ]
        hop2_map, hop2_totals = _batch_hop2_summaries(store, all_caller_ids)
        file_findings: list[Finding] = []
        for i, chunk in enumerate(_build_chunks(spans, max_lines=max_lines)):
            try:
                file_findings.extend(
                    run_agent_a_chunk(
                        store, root, repo, file, chunk, llm,
                        include_preamble=(i == 0),
                        catalog=catalog,
                        _callers_map=callers_map,
                        _hop2_map=hop2_map,
                        _hop2_totals=hop2_totals,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"  [agent_a] skipped chunk {i} of {file}: {exc}"
                if log:
                    log(msg)
                else:
                    print(msg)
        return file_findings

    results: list[list[Finding] | None] = [None] * len(files)
    with ThreadPoolExecutor(max_workers=6) as executor:
        future_to_idx = {executor.submit(_process_file, f): i for i, f in enumerate(files)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:  # noqa: BLE001
                msg = f"  [agent_a] file failed: {files[idx]}: {exc}"
                if log:
                    log(msg)
                else:
                    print(msg)
                results[idx] = []

    return [f for file_findings in results for f in (file_findings or [])]
