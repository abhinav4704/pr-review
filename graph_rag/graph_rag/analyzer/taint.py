"""Agent B — taint composition (transfer functions) + qualify + architecture/
layering, all reading the graph/source directly. No identity or
implementation-flow generation anywhere in this module (that belongs to the
separate `rag/` tool) — per the 2-agent redesign, qualify and the
architecture deep-dive both read the RAW SOURCE of every function in the
relevant chain directly.

This module is a straight restoration + merge of the two original pre-split
modules (`taint.py` + `architecture.py`, recovered from VS Code local file
history after an accidental deletion — NOT a from-memory reconstruction),
with one deliberate change from the originals: the qualify pass (old Agent 3)
and the architecture deep-dive (old Agent 4 Stage 2) used to read each
function's `implementation_flow` (a cached LLM-generated summary from
rag/semantic.py). Per the user's explicit pivot, both now read each
function's actual raw source instead — no flow/identity generation is used
by the analyzer at all.

Phase 3 change: taint composition now reads per-function DFG summaries from
the graph (dfg_json, written at index time by graph_core/dataflow.py) instead
of re-running tree-sitter AST analysis at query time. Sink classification
happens here at walk time via sinks.SinkCatalog, so changing the sink list
no longer requires re-indexing.

Passes, in order:
    1. `find_taint_findings`   — walk DFG facts from each endpoint-handler
                                 param to a sink (graph_proven, no LLM).
    2. `tag_sanitizers`        — one cached LLM call per *candidate* function
                                 only (name suggests validation, or another
                                 function's transfer facts actually call
                                 through it). Reads raw source, not flows.
    3. `run_taint_qualify`     — LLM confirms/denies each composed finding by
                                 reading the RAW SOURCE of every function in
                                 the source->sink chain.
    4. `run_architecture_pass` — Stage 1 (bulk, cheap, one batched call):
                                 collapse call chains from every
                                 endpoint_handler root onto role-sequence
                                 "shapes"; flag risky ones. Stage 2 (targeted,
                                 one call per flagged shape): read the RAW
                                 SOURCE of the shape's representative chain
                                 and produce anchored findings. Plus a fully
                                 deterministic whole-graph cycle sweep
                                 (`find_unreached_cycles`) that catches cycles
                                 invisible to endpoint-rooted walks.

Known v1 limitations (name them, don't hide them):
    - Callee resolution for composition prefers the graph's own resolved
      CALLS edges from the caller when present, falling back to bare-name
      matching only when no resolved edge exists.
    - `*args`/`**kwargs` call-site splats recorded in dfg_json without a
      static-resolvable keyword are skipped by composition (arg_keyword "*"
      or "**" → _resolve_arg_position returns None).
"""
from __future__ import annotations

import json
import os
from collections import defaultdict

from ..graph_core.findings import Finding
from ..graph_core.store import GraphStore
from .context import file_imports_block, shared_state_for_ids
from .sinks import (
    DANGEROUS_EXTERNAL_RECEIVERS,
    SANITIZER_HINTS,
    TAINT_INERT_BUILTINS,
    SinkCatalog,
    load_sinks,
)

# --- sanitizer tagging (one cached LLM call per candidate, reads raw source) -

_SANITIZER_SCHEMA = {
    "type": "object",
    "properties": {
        "sanitizes": {
            "type": "object",
            "description": (
                "vuln class -> list of 0-based parameter indices this function "
                "actually neutralizes for that class specifically."
            ),
            "additionalProperties": {"type": "array", "items": {"type": "integer"}},
        }
    },
    "required": ["sanitizes"],
}

_SANITIZER_SYSTEM = (
    "You are a precise static-analysis assistant. Given one function's signature, "
    "docstring, and RAW SOURCE, decide — per vulnerability class — which parameter "
    "indices it actually neutralizes as a security sanitizer for that class. "
    "A function that only lowercases, trims, or logs does NOT sanitize anything. "
    "Be conservative: if unsure, leave the class out entirely. Vulnerability "
    "classes to consider: sql_injection, command_injection, path_traversal, ssrf, "
    "template_injection, deserialization, eval_injection, xss."
)


def find_sanitizer_candidates(store: GraphStore, repo: str, refresh: bool = False) -> list[dict]:
    """Candidates worth an LLM call: name hints at sanitization, or some other
    function's DFG facts actually name this one as a callee — i.e. composition
    would really walk through it. Language-agnostic (Python + Java both populate
    dfg_json via run_dataflow at index time now)."""
    rows = store.read(
        "MATCH (n:Function {repo:$repo}) WHERE n.lang IN ['python', 'java'] "
        "RETURN n.id AS id, n.name AS name, n.fqn AS fqn, n.signature AS signature, "
        "n.docstring AS docstring, n.file AS file, n.start_line AS start_line, "
        "n.end_line AS end_line, n.body_hash AS body_hash, "
        "n.sanitizer_hash AS sanitizer_hash, n.dfg_json AS dfg_json",
        repo=repo,
    )
    callee_names: set[str] = set()
    for row in rows:
        dj = row.get("dfg_json")
        if not dj:
            continue
        try:
            data = json.loads(dj)
        except (TypeError, ValueError):
            continue
        for af in data.get("passes", []):
            if af.get("callee"):
                callee_names.add(af["callee"])

    out = []
    for row in rows:
        name = row.get("name") or ""
        if not (name in callee_names or SANITIZER_HINTS.search(name)):
            continue
        body_hash = row.get("body_hash") or ""
        if not refresh and body_hash and row.get("sanitizer_hash") == body_hash:
            continue
        out.append(row)
    return out


def _own_sink_params(dfg_json_str: str | None, catalog: SinkCatalog) -> dict[str, set[int]]:
    """vuln_class -> set of param indices this function's OWN sink calls consume.
    Used to reject self-contradictory sanitizer tags: a function whose body IS
    the dangerous sink for (class, param) cannot also be "the sanitizer" for
    that same (class, param)."""
    if not dfg_json_str:
        return {}
    try:
        data = json.loads(dfg_json_str)
    except (TypeError, ValueError):
        return {}
    out: dict[str, set[int]] = {}
    for af in data.get("passes", []):
        if not af.get("from_params"):
            continue
        vuln_class = catalog.classify(af.get("recv", "") or "", af.get("callee", "") or "")
        if vuln_class:
            out.setdefault(vuln_class, set()).update(af["from_params"])
    return out


def _read_source(root: str, file_rel: str, start_line: int | None, end_line: int | None) -> str:
    if not file_rel:
        return ""
    abspath = os.path.join(root, file_rel)
    try:
        with open(abspath, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    start = max((start_line or 1) - 1, 0)
    end = end_line or (start + 1)
    return "".join(lines[start:end])


def tag_sanitizers(store: GraphStore, repo: str, root: str, llm, limit: int | None = None,
                   refresh: bool = False) -> dict:
    catalog = load_sinks(root)
    candidates = find_sanitizer_candidates(store, repo, refresh=refresh)
    if limit is not None:
        candidates = candidates[:limit]

    root = os.path.abspath(root)
    out_rows: list[dict] = []
    tagged = 0
    for row in candidates:
        source = _read_source(root, row.get("file"), row.get("start_line"), row.get("end_line"))
        imports_block = file_imports_block(root, row.get("file") or "")
        shared_state = shared_state_for_ids(store, [row["id"]]) if row.get("id") else ""
        user = (
            f"function: {row.get('fqn') or row.get('name')}\n"
            f"signature: {row.get('signature') or ''}\n"
            f"docstring: {row.get('docstring') or ''}\n"
            + (f"imports in {row.get('file')}:\n{imports_block}\n" if imports_block else "")
            + (f"{shared_state} (real shared state, not a local variable)\n" if shared_state else "")
            + f"source:\n{source}"
        )
        try:
            result = llm.extract(_SANITIZER_SYSTEM, user, _SANITIZER_SCHEMA)
        except Exception:
            continue
        sanitizes = result.get("sanitizes", {}) if isinstance(result, dict) else {}
        own_sinks = _own_sink_params(row.get("dfg_json"), catalog)
        sanitizes = {
            vuln_class: kept
            for vuln_class, idxs in sanitizes.items()
            if (kept := [i for i in idxs if i not in own_sinks.get(vuln_class, set())])
        }
        out_rows.append({
            "id": row["id"],
            "props": {
                "sanitizer_json": json.dumps(sanitizes, sort_keys=True),
                "sanitizer_hash": row.get("body_hash") or "",
            },
        })
        tagged += 1

    if out_rows:
        store.write_semantics(out_rows)
    return {"candidates": len(candidates), "tagged": tagged}


# --- taint composition (deterministic, no LLM) ------------------------------

def _calls_from_map(store: GraphStore, repo: str) -> dict[str, set[str]]:
    """caller node id -> set of resolved callee node ids, from real CALLS edges
    (any confidence) — used to narrow bare-name composition candidates to the
    graph's own resolved targets when available, instead of guessing purely
    by name."""
    rows = store.read(
        "MATCH (a:Function {repo:$repo})-[:CALLS]->(b:Function) "
        "RETURN a.id AS src, b.id AS dst",
        repo=repo,
    )
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["src"], set()).add(r["dst"])
    return out


def _resolve_callees(node_row: dict, name: str, by_name: dict[str, list[dict]],
                     calls_from: dict[str, set[str]]) -> list[dict]:
    """Prefer this caller's real resolved CALLS edges over bare-name matching;
    fall back to bare-name candidates only when no resolved edge narrows it."""
    candidates = by_name.get(name, [])
    if not candidates:
        return []
    edge_targets = calls_from.get(node_row.get("id"), set())
    if edge_targets:
        narrowed = [c for c in candidates if c.get("id") in edge_targets]
        if narrowed:
            return narrowed
    return candidates


def _resolve_arg_position(callee_params: list[str], arg_position, arg_keyword) -> int | None:
    """Map a DFG ArgFlow's arg_position/arg_keyword to the callee's actual
    0-based parameter index. Returns None if it can't be precisely mapped
    (e.g. an unmapped `*args`/`**kwargs` splat) — composition does not cross
    into the callee in that case."""
    if arg_position is not None:
        return arg_position
    if arg_keyword in (None, "*", "**"):
        return None
    if arg_keyword in callee_params:
        return callee_params.index(arg_keyword)
    return None


def _is_taint_source(row: dict) -> bool:
    """A function is an external taint entry point if it handles an HTTP
    endpoint/webhook (component_role endpoint_handler, which the role classifier
    also assigns to controller-class methods) OR it subscribes to an event/
    message queue (a CONSUMES_EVENT edge). Both carry attacker-influenceable
    input from outside the process."""
    return (row.get("component_role") == "endpoint_handler"
            or (row.get("consumes_event") or 0) > 0)


def find_taint_findings(store: GraphStore, repo: str, root: str = "",
                        max_hops: int = 6) -> list[dict]:
    """Walk DFG transfer facts from each external entry-point parameter (HTTP
    endpoint/webhook handler or event/queue consumer) to a sink, stopping a
    branch early if a sanitizer covers that vuln_class on the
    incoming param. Composition runs in Python over one bulk read — still fully
    deterministic. Sinks are classified at walk time via SinkCatalog (no re-index
    needed when the sink list changes)."""
    rows = store.read(
        "MATCH (n:Function {repo:$repo}) "
        "OPTIONAL MATCH (n)-[:WRITES]->(wt:Table) "
        "OPTIONAL MATCH (n)-[:READS]->(rt:Table) "
        "OPTIONAL MATCH (n)-[:CONSUMES_EVENT]->(ev:Event) "
        "WITH n, collect(DISTINCT wt.name) AS writes_tables, "
        "collect(DISTINCT rt.name) AS reads_tables, "
        "count(DISTINCT ev) AS consumes_event "
        "RETURN n.id AS id, n.name AS name, n.fqn AS fqn, n.file AS file, "
        "n.start_line AS start_line, n.param_names AS param_names, "
        "n.component_role AS component_role, n.dfg_json AS dfg_json, "
        "n.sanitizer_json AS sanitizer_json, "
        "writes_tables, reads_tables, consumes_event",
        repo=repo,
    )
    catalog = load_sinks(root)
    by_name: dict[str, list[dict]] = {}
    for row in rows:
        by_name.setdefault(row.get("name") or "", []).append(row)
    calls_from = _calls_from_map(store, repo)

    findings: list[dict] = []
    seen: set[tuple] = set()
    tainted_fields: set[str] = set()
    for row in rows:
        if not _is_taint_source(row) or not row.get("dfg_json"):
            continue
        n_params = len(row.get("param_names") or [])
        for i in range(n_params):
            _walk_taint(row, i, [row.get("fqn") or row.get("name")], by_name,
                        findings, seen, max_hops, calls_from, catalog, tainted_fields)
    # Cross-method sweep for instance fields tainted by an entry-point param.
    if tainted_fields:
        _propagate_field_taint(rows, tainted_fields, by_name, findings, seen,
                               max_hops, calls_from, catalog)
    return findings


def enumerate_taint_paths(
    store: GraphStore,
    repo: str,
    root: str = "",
    max_hops: int = 8,
    max_paths: int = 50000,
    include_sanitized: bool = True,
) -> list[dict]:
    """Enumerate endpoint-origin taint paths to sinks.

    Returns one record per reachable sink event with the full function chain:
    source endpoint function/param -> ... -> sink function/callee/line.
    """
    rows = store.read(
        "MATCH (n:Function {repo:$repo}) "
        "OPTIONAL MATCH (n)-[:WRITES]->(wt:Table) "
        "OPTIONAL MATCH (n)-[:READS]->(rt:Table) "
        "OPTIONAL MATCH (n)-[:CONSUMES_EVENT]->(ev:Event) "
        "WITH n, collect(DISTINCT wt.name) AS writes_tables, "
        "collect(DISTINCT rt.name) AS reads_tables, "
        "count(DISTINCT ev) AS consumes_event "
        "RETURN n.id AS id, n.name AS name, n.fqn AS fqn, n.file AS file, "
        "n.start_line AS start_line, n.param_names AS param_names, "
        "n.component_role AS component_role, n.dfg_json AS dfg_json, "
        "n.sanitizer_json AS sanitizer_json, "
        "writes_tables, reads_tables, consumes_event",
        repo=repo,
    )
    catalog = load_sinks(root)
    by_name: dict[str, list[dict]] = {}
    for row in rows:
        by_name.setdefault(row.get("name") or "", []).append(row)
    calls_from = _calls_from_map(store, repo)

    out: list[dict] = []
    seen: set[tuple] = set()
    for row in rows:
        if not _is_taint_source(row) or not row.get("dfg_json"):
            continue
        params = row.get("param_names") or []
        for i, pname in enumerate(params):
            _walk_taint_paths(
                node_row=row,
                param_idx=i,
                source_row=row,
                source_param_name=pname or f"param_{i}",
                chain=[row.get("fqn") or row.get("name")],
                by_name=by_name,
                out=out,
                seen=seen,
                hops_left=max_hops,
                include_sanitized=include_sanitized,
                max_paths=max_paths,
                calls_from=calls_from,
                catalog=catalog,
            )
            if len(out) >= max_paths:
                out.sort(
                    key=lambda p: (
                        p.get("vuln_class") or "",
                        p.get("source_fqn") or "",
                        p.get("sink_fqn") or "",
                        p.get("sink_line") or 0,
                    )
                )
                return out
    out.sort(
        key=lambda p: (
            p.get("vuln_class") or "",
            p.get("source_fqn") or "",
            p.get("sink_fqn") or "",
            p.get("sink_line") or 0,
        )
    )
    return out


def _walk_taint_paths(
    node_row: dict,
    param_idx: int,
    source_row: dict,
    source_param_name: str,
    chain: list[str],
    by_name: dict[str, list[dict]],
    out: list[dict],
    seen: set[tuple],
    hops_left: int,
    include_sanitized: bool,
    max_paths: int,
    calls_from: dict[str, set[str]],
    catalog: SinkCatalog,
) -> None:
    if hops_left <= 0 or len(out) >= max_paths:
        return
    dfg_json = node_row.get("dfg_json")
    if not dfg_json:
        return
    try:
        data = json.loads(dfg_json)
    except (TypeError, ValueError):
        return

    node_fqn = node_row.get("fqn") or node_row.get("name")
    source_fqn = source_row.get("fqn") or source_row.get("name")

    for af in data.get("passes", []):
        if param_idx not in af.get("from_params", []):
            continue
        recv = af.get("recv", "") or ""
        callee_name = af.get("callee") or ""
        if not callee_name:
            continue

        vuln_class = catalog.classify(recv, callee_name)
        if vuln_class:
            sanitized = _is_sanitized(node_row, vuln_class, param_idx)
            if sanitized and not include_sanitized:
                continue
            callee_str = f"{recv}.{callee_name}" if recv else callee_name
            key = (
                source_row.get("id"),
                source_param_name,
                node_row.get("id"),
                param_idx,
                vuln_class,
                af.get("line", 0),
                tuple(chain),
                bool(sanitized),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "source_fqn": source_fqn,
                "source_file": source_row.get("file"),
                "source_line": source_row.get("start_line"),
                "source_param_index": param_idx,
                "source_param_name": source_param_name,
                "vuln_class": vuln_class,
                "sink_fqn": node_fqn,
                "sink_file": node_row.get("file"),
                "sink_line": af.get("line"),
                "sink_callee": callee_str,
                "path": list(chain),
                "path_hops": max(len(chain) - 1, 0),
                "sanitized": bool(sanitized),
                "status": "sanitized_on_path" if sanitized else "unsanitized_reach",
            })
            if len(out) >= max_paths:
                return
        else:
            callee_rows = _resolve_callees(node_row, callee_name, by_name, calls_from)
            if (not callee_rows and callee_name and callee_name not in TAINT_INERT_BUILTINS
                    and recv and recv.lower() in DANGEROUS_EXTERNAL_RECEIVERS):
                key = (
                    source_row.get("id"), source_param_name, node_row.get("id"),
                    param_idx, "external_unresolved", callee_name, tuple(chain),
                )
                if key not in seen:
                    seen.add(key)
                    callee_str = f"{recv}.{callee_name}" if recv else callee_name
                    out.append({
                        "source_fqn": source_fqn,
                        "source_file": source_row.get("file"),
                        "source_line": source_row.get("start_line"),
                        "source_param_index": param_idx,
                        "source_param_name": source_param_name,
                        "vuln_class": None,
                        "sink_fqn": None,
                        "sink_file": node_row.get("file"),
                        "sink_line": None,
                        "sink_callee": callee_str,
                        "path": list(chain),
                        "path_hops": max(len(chain) - 1, 0),
                        "sanitized": False,
                        "status": "external_unresolved",
                        "external": True,
                        "confidence": "LOW",
                    })
            for callee_row in callee_rows:
                callee_fqn = callee_row.get("fqn") or callee_row.get("name")
                if callee_fqn in chain:
                    continue
                callee_params = callee_row.get("param_names") or []
                resolved_pos = _resolve_arg_position(
                    callee_params, af.get("arg_position"), af.get("arg_keyword")
                )
                if resolved_pos is None or resolved_pos >= len(callee_params):
                    continue
                _walk_taint_paths(
                    node_row=callee_row,
                    param_idx=resolved_pos,
                    source_row=source_row,
                    source_param_name=source_param_name,
                    chain=chain + [callee_fqn],
                    by_name=by_name,
                    out=out,
                    seen=seen,
                    hops_left=hops_left - 1,
                    include_sanitized=include_sanitized,
                    max_paths=max_paths,
                    calls_from=calls_from,
                    catalog=catalog,
                )
                if len(out) >= max_paths:
                    return


def _walk_taint(node_row: dict, param_idx: int, path: list[str],
                by_name: dict[str, list[dict]],
                findings: list[dict], seen: set[tuple], hops_left: int,
                calls_from: dict[str, set[str]], catalog: SinkCatalog,
                tainted_fields: set[str] | None = None) -> None:
    if hops_left <= 0:
        return
    dfg_json = node_row.get("dfg_json")
    if not dfg_json:
        return
    try:
        data = json.loads(dfg_json)
    except (TypeError, ValueError):
        return

    # Field-taint seed: if the tainted param is stored to self.<field>, that
    # (class-qualified) field becomes tainted for the cross-method sweep that
    # runs after the param walk. Names match ArgFlow.from_fields exactly.
    if tainted_fields is not None:
        for fw in data.get("field_writes", []):
            if param_idx in (fw.get("from_params") or []):
                fld = fw.get("field")
                if fld:
                    tainted_fields.add(fld)

    for af in data.get("passes", []):
        if param_idx not in af.get("from_params", []):
            continue
        recv = af.get("recv", "") or ""
        callee_name = af.get("callee") or ""
        if not callee_name:
            continue

        vuln_class = catalog.classify(recv, callee_name)
        if vuln_class:
            if _is_sanitized(node_row, vuln_class, param_idx):
                continue
            key = (node_row["id"], vuln_class, af.get("line", 0), tuple(path))
            if key in seen:
                continue
            seen.add(key)
            callee_str = f"{recv}.{callee_name}" if recv else callee_name
            # Enrich SQL findings with affected table names when the graph knows them.
            tables = [t for t in (node_row.get("writes_tables") or []) if t]
            table_suffix = f" [tables: {', '.join(sorted(tables))}]" if (tables and vuln_class in ("sql_injection", "nosql_injection")) else ""
            findings.append({
                "category": "security",
                "subcategory": vuln_class,
                "source": "graph_proven",
                "owning_fqn": node_row.get("fqn") or node_row.get("name"),
                "file": node_row.get("file"),
                "line": af.get("line") or node_row.get("start_line") or 0,
                "message": (
                    f"{vuln_class}: untrusted input reaches {callee_str} "
                    f"via {' -> '.join(path)}{table_suffix}"
                ),
                "path": list(path),
                **({"tables": tables} if tables else {}),
            })
        else:
            callee_rows = _resolve_callees(node_row, callee_name, by_name, calls_from)
            if (not callee_rows and callee_name and callee_name not in TAINT_INERT_BUILTINS
                    and recv and recv.lower() in DANGEROUS_EXTERNAL_RECEIVERS):
                key = (node_row["id"], "external_unresolved", callee_name, tuple(path))
                if key not in seen:
                    seen.add(key)
                    callee_str = f"{recv}.{callee_name}" if recv else callee_name
                    findings.append({
                        "category": "security",
                        "subcategory": "unresolved_external_taint_flow",
                        "source": "graph_proven",
                        "owning_fqn": node_row.get("fqn") or node_row.get("name"),
                        "file": node_row.get("file"),
                        "line": node_row.get("start_line"),
                        "message": (
                            f"tainted value reaches {callee_str} "
                            f"via {' -> '.join(path)} — cannot trace further into "
                            f"this external library (may reach a sink internally)."
                        ),
                        "path": list(path),
                        "external": True,
                        "confidence": "LOW",
                    })
            for callee_row in callee_rows:
                callee_fqn = callee_row.get("fqn") or callee_row.get("name")
                if callee_fqn in path:
                    continue

                # Graph-proven sink: callee function has WRITES edges to SQL
                # tables (derived at index time by _derive_sql_links). Catches
                # ORM methods that aren't in the name-based SinkCatalog.
                callee_tables = [t for t in (callee_row.get("writes_tables") or []) if t]
                if callee_tables:
                    if not _is_sanitized(node_row, "sql_injection", param_idx):
                        key = (node_row["id"], "sql_injection", af.get("line", 0), tuple(path))
                        if key not in seen:
                            seen.add(key)
                            tables_str = ", ".join(sorted(callee_tables))
                            callee_str = f"{recv}.{callee_name}" if recv else callee_name
                            findings.append({
                                "category": "security",
                                "subcategory": "sql_injection",
                                "source": "graph_proven",
                                "owning_fqn": node_row.get("fqn") or node_row.get("name"),
                                "file": node_row.get("file"),
                                "line": af.get("line") or node_row.get("start_line") or 0,
                                "message": (
                                    f"sql_injection: untrusted input reaches {callee_str} "
                                    f"which writes to table(s) [{tables_str}] "
                                    f"via {' -> '.join(path)}"
                                ),
                                "path": list(path),
                                "tables": callee_tables,
                            })
                    continue

                callee_params = callee_row.get("param_names") or []
                resolved_pos = _resolve_arg_position(
                    callee_params, af.get("arg_position"), af.get("arg_keyword")
                )
                if resolved_pos is None or resolved_pos >= len(callee_params):
                    continue
                _walk_taint(callee_row, resolved_pos, path + [callee_fqn], by_name,
                            findings, seen, hops_left - 1, calls_from, catalog,
                            tainted_fields)


def _propagate_field_taint(rows: list[dict], tainted_fields: set[str],
                           by_name: dict[str, list[dict]],
                           findings: list[dict], seen: set[tuple], max_hops: int,
                           calls_from: dict[str, set[str]], catalog: SinkCatalog) -> None:
    """Second, cross-method sweep after the param walk. A field tainted by an
    entry-point param (self.x = param in one method) reaching a sink through a
    DIFFERENT method (conn.execute(self.x)) is invisible to the param walk,
    which only follows call arguments. Here we fix-point over the tainted-field
    set: any method whose ArgFlow reads a tainted field into a sink is a finding;
    a method that stores a tainted field into another self.field grows the set;
    a tainted field passed as a call argument re-enters the param walk on the
    callee. Field names are class-qualified, so matches never cross classes."""
    # Fixed point: keep sweeping until no new field becomes tainted.
    changed = True
    guard = 0
    while changed and guard < 50:
        guard += 1
        changed = False
        for row in rows:
            dfg = row.get("dfg_json")
            if not dfg:
                continue
            try:
                data = json.loads(dfg)
            except (TypeError, ValueError):
                continue
            method_fqn = row.get("fqn") or row.get("name")

            for af in data.get("passes", []):
                hit_fields = set(af.get("from_fields") or []) & tainted_fields
                if not hit_fields:
                    continue
                recv = af.get("recv", "") or ""
                callee_name = af.get("callee") or ""
                if not callee_name:
                    continue
                vuln_class = catalog.classify(recv, callee_name)
                if vuln_class:
                    key = (row["id"], vuln_class, af.get("line", 0), "field")
                    if key in seen:
                        continue
                    seen.add(key)
                    callee_str = f"{recv}.{callee_name}" if recv else callee_name
                    fld = sorted(hit_fields)[0]
                    findings.append({
                        "category": "security",
                        "subcategory": vuln_class,
                        "source": "graph_proven",
                        "owning_fqn": method_fqn,
                        "file": row.get("file"),
                        "line": af.get("line") or row.get("start_line") or 0,
                        "message": (
                            f"{vuln_class}: tainted instance field `{fld}` (set from "
                            f"untrusted input in another method) reaches {callee_str} "
                            f"in {method_fqn}"
                        ),
                        "path": [method_fqn],
                        "via_field": fld,
                    })
                else:
                    # Tainted field passed into a helper — re-enter the param walk.
                    callee_rows = _resolve_callees(row, callee_name, by_name, calls_from)
                    for callee_row in callee_rows:
                        callee_params = callee_row.get("param_names") or []
                        resolved_pos = _resolve_arg_position(
                            callee_params, af.get("arg_position"), af.get("arg_keyword")
                        )
                        if resolved_pos is None or resolved_pos >= len(callee_params):
                            continue
                        _walk_taint(callee_row, resolved_pos,
                                    [method_fqn, callee_row.get("fqn") or callee_row.get("name")],
                                    by_name, findings, seen, max_hops - 1,
                                    calls_from, catalog, tainted_fields)

            # field -> field: storing a tainted field into another self.field.
            for fw in data.get("field_writes", []):
                if set(fw.get("from_fields") or []) & tainted_fields:
                    fld = fw.get("field")
                    if fld and fld not in tainted_fields:
                        tainted_fields.add(fld)
                        changed = True


def _is_sanitized(node_row: dict, vuln_class: str, param_idx: int) -> bool:
    sanitizer_json = node_row.get("sanitizer_json")
    if not sanitizer_json:
        return False
    try:
        san = json.loads(sanitizer_json)
    except (TypeError, ValueError):
        return False
    return param_idx in san.get(vuln_class, [])


# --- Agent B, pass 2: LLM qualify over composed taint findings --------------
#
# Reads the RAW SOURCE of every function in the source->sink chain directly
# (not a cached `implementation_flow` summary — that belongs to the separate
# rag/ tool only). Purely additive: does not touch `find_taint_findings`'s
# deterministic graph_proven output.

_QUALIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["true_positive", "false_positive", "needs_more_context"],
        },
        "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "message": {"type": "string"},
        "evidence": {"type": "string"},
        "recommendation": {"type": "string"},
    },
    "required": ["verdict", "confidence", "message"],
}

_QUALIFY_SYSTEM = (
    "You are verifying ONE proposed taint-flow security finding: untrusted input from a source "
    "parameter is claimed to reach a sink through a chain of function calls. This chain was "
    "already deterministically confirmed by static analysis (tree-sitter) to pass the tainted "
    "value into the sink with no sanitizer detected along the way — your job is to read the "
    "ACTUAL RAW SOURCE of every function in the chain (in order from source to sink) and check "
    "whether it reveals a sanitization/validation/escaping/parameterization step that the static "
    "pass might have missed, not to re-derive reachability from scratch. "
    "Default to 'true_positive' when the source code shows the value being passed straight "
    "through (string formatting, concatenation, direct parameter forwarding) with no explicit "
    "validation/sanitization/escaping/parameterization step anywhere in the chain — absence of a "
    "visible safeguard IS the signal. Only set verdict to 'false_positive' if the code explicitly "
    "sanitizes, validates, escapes, or parameterizes the value before the sink. Only use "
    "'needs_more_context' if a function's source is missing/unreadable AND that function sits "
    "between the source and the sink where a sanitizer could plausibly live. Do not invent "
    "behavior not shown in the given source."
)


def _chain_source_blocks(root: str, fqns: list[str], by_fqn: dict,
                          store: GraphStore | None = None) -> list[str]:
    """Read the raw source of every function in `fqns`, in chain order —
    plus, no matter what, that function's file imports and any real
    class/module-level shared state it reads/writes, since a sanitizer/guard
    can live behind an imported helper or a shared flag the raw span alone
    doesn't show."""
    root = os.path.abspath(root)
    lines = []
    for fqn in fqns:
        row = by_fqn.get(fqn, {})
        role = row.get("component_role") or "unknown"
        source = _read_source(root, row.get("file"), row.get("start_line"), row.get("end_line"))
        extra = []
        imports_block = file_imports_block(root, row.get("file") or "")
        if imports_block:
            extra.append(f"imports in {row.get('file')}:\n{imports_block}")
        if store is not None and row.get("id"):
            shared_state = shared_state_for_ids(store, [row["id"]])
            if shared_state:
                extra.append(f"{shared_state} (real shared state, not a local variable)")
        extra_block = ("\n" + "\n".join(extra) + "\n") if extra else ""
        lines.append(
            f"--- {fqn} (role={role}) ---\n{extra_block}{source or '(source unavailable)'}"
        )
    return lines


def qualify_taint_finding(store: GraphStore, repo: str, root: str, llm,
                          finding: dict, by_fqn: dict) -> Finding | None:
    """One finding: send the chain's raw source to the LLM and return a
    `Finding` carrying its verdict, or None if the LLM says false_positive
    (nothing to report) or llm is missing."""
    if llm is None:
        return None
    fqns = finding.get("path") or [finding.get("owning_fqn")]
    chain_lines = _chain_source_blocks(root, fqns, by_fqn, store=store)

    user = (
        f"vuln_class: {finding.get('subcategory')}\n"
        f"claimed sink: {finding.get('owning_fqn')} ({finding.get('file')}:{finding.get('line')})\n\n"
        f"chain (source -> sink), raw code:\n" + "\n\n".join(chain_lines)
    )
    result = llm.extract(_QUALIFY_SYSTEM, user, _QUALIFY_SCHEMA)
    if not isinstance(result, dict):
        return None
    verdict = result.get("verdict")
    if verdict != "true_positive":
        return None

    confidence = str(result.get("confidence", "MEDIUM")).upper()
    message = str(result.get("message") or finding.get("message") or "").strip()
    if not message:
        return None
    return Finding(
        category="security",
        subcategory=finding.get("subcategory", "unspecified"),
        source="llm_judged",
        owning_fqn=finding.get("owning_fqn", ""),
        file=finding.get("file", ""),
        line=int(finding.get("line") or 0),
        message=message,
        evidence=str(result.get("evidence", "")).strip(),
        recommendation=str(result.get("recommendation", "")).strip(),
        confidence=confidence if confidence in ("HIGH", "MEDIUM", "LOW") else "MEDIUM",
    )


def run_taint_qualify(store: GraphStore, repo: str, root: str, llm,
                      max_hops: int = 6, limit: int | None = None) -> list[Finding]:
    """Run the deterministic composition, then ask the LLM to confirm/deny
    each finding by reading the chain's raw source. Only LLM-confirmed
    true_positive findings are returned — a precision filter layered on top
    of `find_taint_findings`'s recall, not a replacement for it."""
    raw_findings = find_taint_findings(store, repo, root=root, max_hops=max_hops)
    raw_findings = [f for f in raw_findings if not f.get("external")]
    if limit is not None:
        raw_findings = raw_findings[:limit]
    by_fqn = {r["fqn"]: r for r in store.read(
        "MATCH (n:Function {repo:$repo}) RETURN n.id AS id, n.fqn AS fqn, n.file AS file, "
        "n.start_line AS start_line, n.end_line AS end_line, "
        "n.component_role AS component_role, n.role_confidence AS role_confidence",
        repo=repo,
    ) if r.get("fqn")}

    out: list[Finding] = []
    for finding in raw_findings:
        f = qualify_taint_finding(store, repo, root, llm, finding, by_fqn)
        if f is not None:
            out.append(f)
    return out


# --- Agent B: free-form security DISCOVERY (reads reachable chains) ---------
#
# Distinct from run_taint_qualify (which only CONFIRMS a specific deterministic
# finding under its pre-decided class). This reviewer reads the full raw source
# of a reachable endpoint->sink chain and reports ANY security vulnerability it
# can prove — no fixed vuln vocabulary. The deterministic taint findings for the
# chain are handed in as HIGH-confidence EVIDENCE to anchor precision, not as the
# only thing it may report. Per the confirmed 2026 redesign: "discovery + evidence".

_SECURITY_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "owning_fqn": {
                        "type": "string",
                        "description": "the function in the chain where the vulnerability lives (exact fqn as shown)",
                    },
                    "subcategory": {
                        "type": "string",
                        "description": (
                            "concise snake_case name of the SPECIFIC vulnerability, e.g. "
                            "sql_injection, path_traversal, ssrf, insecure_deserialization, "
                            "missing_authorization, broken_object_level_authorization, "
                            "server_side_template_injection. Pick the most precise name — "
                            "do NOT force it into a preset list."
                        ),
                    },
                    "line": {"type": "integer"},
                    "message": {"type": "string"},
                    "evidence": {"type": "string", "description": "quote the actual vulnerable code"},
                    "recommendation": {"type": "string"},
                    "severity": {
                        "type": "number",
                        "description": "how bad IF real, 0-10 (ignore reachability — scoring adds that)",
                    },
                    "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                },
                "required": ["owning_fqn", "subcategory", "line", "message", "evidence", "severity", "confidence"],
            },
        }
    },
    "required": ["findings"],
}

_SECURITY_SYSTEM = (
    "You are a senior application-security engineer reviewing a REACHABLE call chain in a "
    "codebase — a sequence of functions from an external entry point (endpoint/webhook/queue "
    "handler) down through the code it calls. You are given the RAW SOURCE of every function "
    "in the chain, in order, plus its imports and any real shared state.\n\n"
    "Find EVERY security vulnerability you can prove from this source — do not limit yourself "
    "to any preset list of vulnerability types, and name each one with the most precise "
    "snake_case subcategory you can (e.g. sql_injection, path_traversal, ssrf, "
    "insecure_deserialization, xxe, open_redirect, server_side_template_injection, "
    "missing_authorization, broken_object_level_authorization, hardcoded_secret, weak_crypto, "
    "sensitive_data_exposure, mass_assignment — or a better name if none of these fit).\n\n"
    "EVIDENCE: some flows in this chain were already proven by static taint analysis "
    "(tree-sitter DFG) to carry untrusted input into a sink with no sanitizer detected. Those "
    "are listed as GRAPH-PROVEN EVIDENCE below. Treat them as high-confidence starting points: "
    "confirm and sharpen them (they are real unless the source shows a sanitizer/validator/"
    "parameterization the static pass missed), AND look beyond them for additional issues the "
    "static pass could not classify — an uncatalogued/ORM/framework sink, a missing "
    "authorization check before a sensitive operation, an object reference taken straight from "
    "user input, unsafe deserialization, etc.\n\n"
    "Judge from the SOURCE, not from names: a value passed straight through (f-string, "
    "concat, direct forwarding) into a dangerous operation with no visible "
    "validation/escaping/parameterization is a real finding — absence of a safeguard IS the "
    "signal. If the code explicitly sanitizes/validates/parameterizes before the dangerous "
    "use, do NOT report it.\n\n"
    "SEVERITY: set `severity` 0-10 = how bad IF real, from the vuln class alone (RCE/SQLi/"
    "deserialization ~9, path traversal/SSRF ~7, missing authz ~8, info leak ~5). Ignore "
    "reachability — the scorer multiplies blast radius in afterward.\n\n"
    "For each finding set `owning_fqn` to the exact function in the chain where the bug lives, "
    "`line` to the vulnerable line, and quote the code in `evidence`. Report each distinct "
    "issue ONCE (not once per function it passes through). If the chain has no provable "
    "security issue, return an EMPTY findings array — do not invent findings."
)


def review_chain_security(store: GraphStore, repo: str, root: str, llm,
                          fqns: list[str], by_fqn: dict,
                          evidence: list[dict]) -> list[Finding]:
    """Read the whole chain's raw source + deterministic evidence and return
    free-form security Findings (any vuln class, LLM-assigned severity)."""
    if llm is None or not fqns:
        return []
    chain_lines = _chain_source_blocks(root, fqns, by_fqn, store=store)

    if evidence:
        ev_lines = "\n".join(
            f"- [{e.get('subcategory')}] sink at {e.get('owning_fqn')} "
            f"({e.get('file')}:{e.get('line')}): {e.get('message')}"
            for e in evidence
        )
        ev_block = f"GRAPH-PROVEN EVIDENCE (static taint, high confidence):\n{ev_lines}\n\n"
    else:
        ev_block = "GRAPH-PROVEN EVIDENCE: none for this chain — review it fresh.\n\n"

    user = (
        f"reachable chain (entry -> ... -> deepest callee): {' -> '.join(fqns)}\n\n"
        + ev_block
        + "chain source (in order), raw code:\n" + "\n\n".join(chain_lines)
    )
    result = llm.extract(_SECURITY_SYSTEM, user, _SECURITY_SCHEMA)
    items = result.get("findings", []) if isinstance(result, dict) else []
    out: list[Finding] = []
    for item in items:
        owning_fqn = str(item.get("owning_fqn") or fqns[-1])
        row = by_fqn.get(owning_fqn, {})
        f = Finding.from_dict(item, category="security", source="llm_judged",
                              owning_fqn=owning_fqn, file=row.get("file", ""))
        if f:
            if not f.line:
                f.line = row.get("start_line") or 0
            out.append(f)
    return out


def run_agent_b_security(store: GraphStore, repo: str, root: str, llm,
                         max_hops: int = 6, limit: int | None = None) -> list[Finding]:
    """Free-form security discovery over every reachable endpoint->sink chain.

    Enumerates the reachable attack surface (endpoint/webhook/queue-rooted chains
    that reach a sink, sanitized or not), dedups to unique function-chains, and
    hands each chain's full source + its deterministic taint findings (as
    evidence) to the LLM, which reports ANY provable vulnerability. Replaces the
    old confirm-only qualify pass; the deterministic findings still flow through
    separately as graph_proven, so this is additive recall, not a filter."""
    if llm is None:
        return []

    det = find_taint_findings(store, repo, root=root, max_hops=max_hops)
    evidence_by_chain: dict[tuple, list[dict]] = defaultdict(list)
    for f in det:
        if f.get("external"):
            continue
        key = tuple(f.get("path") or [f.get("owning_fqn")])
        evidence_by_chain[key].append(f)

    # Reachable chains (unique fqn tuples) from endpoint enumeration, including
    # sanitized ones — a chain the static pass thinks is clean can still hide a
    # vuln class the catalog doesn't model.
    paths = enumerate_taint_paths(store, repo, root=root, max_hops=max_hops,
                                  include_sanitized=True)
    chains: dict[tuple, list[str]] = {}
    for p in paths:
        fqns = p.get("path") or []
        if fqns:
            chains[tuple(fqns)] = fqns
    for key in evidence_by_chain:
        chains.setdefault(key, list(key))

    # Longer chains first (more surface, more likely to hide cross-function bugs).
    chain_list = sorted(chains.values(), key=len, reverse=True)
    if limit is not None:
        chain_list = chain_list[:limit]

    by_fqn = _all_functions_by_fqn(store, repo)

    out: list[Finding] = []
    for fqns in chain_list:
        evidence = evidence_by_chain.get(tuple(fqns), [])
        out.extend(review_chain_security(store, repo, root, llm, fqns, by_fqn, evidence))
    return out


# --- Agent B: architecture / layering — two-stage path-shape analysis ------
#
# Stage 1 (bulk, cheap, ONE batched call): walk CALLS from every
# `component_role=="endpoint_handler"` root, collapsing on the *role
# sequence* ("shape": e.g. `controller -> service -> repository -> entity`)
# rather than raw function identity — architectural smell is a property of
# the role pattern, not of any one function, so hundreds of concrete chains
# collapse onto a handful of shapes. Feed the LLM the whole shape catalogue
# (shape + count + a few representative concrete instances, each with a
# short docstring snippet — cheap graph reads, no extra LLM calls) in one
# call; it returns which shape-ids look architecturally/security risky.
#
# Stage 2 (targeted, one call per flagged shape): for each flagged shape's
# representative instance, read the RAW SOURCE of every function in that
# chain directly and ask a focused call to produce real findings anchored to
# a specific function in that chain.
#
# Plus a fully deterministic whole-graph cycle sweep (`find_unreached_cycles`)
# that catches cycles invisible to endpoint-rooted walks (no controller/route
# ever calls into them).

# Agent C's classification is FREE-FORM (no enforced enum). These names remain
# only as illustrative examples surfaced in the Stage-2 prompt/schema and as the
# subcategories the deterministic cycle/depth sweep emits directly; they are NOT
# a whitelist. SEVERITY_BASE covers the known ones for the graph_proven path.
_DESIGN_SUBCATS = [
    "layering_violation", "missing_authorization",
    "circular_architectural_dependency", "unclear_ownership",
]

MAX_DEPTH = 8
MAX_EXAMPLES_PER_SHAPE = 3
MAX_SHAPES_TO_LLM = 60
_FANOUT_CAP = 12
_MAX_WALK_STEPS = 20000

CYCLE_MARK = "<cycle>"
DEEP_MARK = "<max_depth>"


def _all_functions(store: GraphStore, repo: str) -> dict:
    rows = store.read(
        "MATCH (n:Function {repo:$repo}) RETURN n.id AS id, n.fqn AS fqn, n.file AS file, "
        "n.start_line AS start_line, n.end_line AS end_line, n.component_role AS component_role, "
        "n.role_confidence AS role_confidence, n.docstring AS docstring",
        repo=repo,
    )
    return {r["id"]: r for r in rows}


def _all_functions_by_fqn(store: GraphStore, repo: str) -> dict:
    rows = store.read(
        "MATCH (n:Function {repo:$repo}) RETURN n.id AS id, n.fqn AS fqn, n.file AS file, "
        "n.start_line AS start_line, n.end_line AS end_line, n.component_role AS component_role, "
        "n.role_confidence AS role_confidence, n.docstring AS docstring",
        repo=repo,
    )
    return {r["fqn"]: r for r in rows}


def _adjacency(store: GraphStore, repo: str) -> dict:
    rows = store.read(
        "MATCH (a:Function {repo:$repo})-[:CALLS]->(b:Function {repo:$repo}) "
        "RETURN DISTINCT a.id AS src, b.id AS dst",
        repo=repo,
    )
    adj: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        adj[r["src"]].append(r["dst"])
    return adj


def _record(shapes: dict, roles_path: list, fqn_path: list, tail_fqn: str | None = None) -> None:
    key = tuple(roles_path)
    entry = shapes.setdefault(key, {"roles": list(roles_path), "count": 0, "examples": []})
    entry["count"] += 1
    if len(entry["examples"]) < MAX_EXAMPLES_PER_SHAPE:
        entry["examples"].append(list(fqn_path) + ([tail_fqn] if tail_fqn else []))


def collect_path_shapes(store: GraphStore, repo: str, max_depth: int = MAX_DEPTH) -> dict:
    """Returns {shape_key(tuple of roles): {"roles": [...], "count": n,
    "examples": [[fqn, ...], ...]}}. Deterministic, no LLM."""
    nodes = _all_functions(store, repo)
    adj = _adjacency(store, repo)
    roots = [nid for nid, n in nodes.items() if n.get("component_role") == "endpoint_handler"]

    shapes: dict[tuple, dict] = {}
    steps_used = 0
    stack = [(r, [], [], frozenset({r})) for r in roots]

    while stack and steps_used < _MAX_WALK_STEPS:
        node_id, roles_path, fqn_path, visited = stack.pop()
        steps_used += 1
        n = nodes.get(node_id)
        if n is None:
            continue
        role = n.get("component_role") or "unknown"
        roles_path = roles_path + [role]
        fqn_path = fqn_path + [n["fqn"]]

        if len(roles_path) >= max_depth:
            _record(shapes, roles_path + [DEEP_MARK], fqn_path)
            continue

        children = [c for c in adj.get(node_id, [])[:_FANOUT_CAP] if c in nodes]
        if not children:
            _record(shapes, roles_path, fqn_path)
            continue

        for c in children:
            if c in visited:
                _record(shapes, roles_path + [CYCLE_MARK], fqn_path, tail_fqn=nodes[c]["fqn"])
                continue
            stack.append((c, roles_path, fqn_path, visited | {c}))

    return shapes


_STAGE1_SCHEMA = {
    "type": "object",
    "properties": {
        "risky_shape_ids": {
            "type": "array",
            "description": "ids (from the given catalogue) of shapes that look "
                            "architecturally or security risky",
            "items": {"type": "string"},
        }
    },
    "required": ["risky_shape_ids"],
}

_STAGE1_SYSTEM = (
    "You are reviewing a catalogue of call-chain SHAPES in a codebase, where a shape is the "
    "sequence of architectural roles a request path passes through (e.g. "
    "controller -> service -> repository -> entity), collapsed from many concrete call chains "
    "that share the same role pattern. You are given each shape's role sequence, how many "
    "concrete chains share it, and a few representative concrete function chains. "
    "Infer this repo's normal/expected layering convention from what's actually common (the "
    "highest-count shapes), then flag shape-ids that deviate from it in a way that suggests a "
    "real architecture or security problem: skipped layers (e.g. a controller reaching a "
    "repository/entity directly with no service in between), backwards calls (e.g. a repository "
    "calling back into a controller), unusually deep chains, or a `<cycle>`/`<max_depth>` marker "
    "at the end of the shape. Do not flag a shape just because it is rare if it still respects "
    "the repo's evident convention — only flag genuine deviations. "
    "IMPORTANT: role labels are produced by a heuristic classifier (name suffix, package path, "
    "or framework annotations), not verified ground truth — each role in a chain example is shown "
    "with a confidence (HIGH/MEDIUM/LOW). An `unknown`/unlabeled role, or any MEDIUM/LOW-confidence "
    "role, is very often just an ordinary function the classifier couldn't confidently label (e.g. "
    "a module-level utility function) — never flag a shape as risky ONLY because it contains an "
    "unknown/unclear-role step; that alone is not a layering violation. Only flag it if there is a "
    "HIGH-confidence role actually being skipped or contradicted (e.g. a HIGH-confidence controller "
    "calling a HIGH-confidence repository with no service anywhere in between)."
)


def _short_desc(row: dict) -> str:
    doc = (row.get("docstring") or "").strip()
    if not doc:
        return ""
    first_line = doc.splitlines()[0].strip()
    return first_line[:100]


def _describe_chain(fqns: list[str], by_fqn: dict) -> str:
    parts = []
    for fqn in fqns:
        row = by_fqn.get(fqn, {})
        desc = _short_desc(row)
        conf = row.get("role_confidence") or "?"
        bits = [b for b in (desc, f"role_conf={conf}") if b]
        parts.append(f"{fqn} ({', '.join(bits)})" if bits else fqn)
    return " -> ".join(parts)


def flag_risky_shapes(shapes: dict, llm, by_fqn: dict | None = None,
                       limit: int | None = None) -> set:
    """Stage 1: one batched LLM call. Returns the set of risky shape KEYS
    (tuples). Returns an empty set if there are no shapes or no llm.

    Shows the most-common shapes (real signal about the repo's convention) AND
    a long-tail sample of the rarest ones — a shape that's both rare AND risky
    would otherwise be silently truncated away by a pure count-descending
    cutoff.
    """
    if not shapes or llm is None:
        return set()
    by_fqn = by_fqn or {}
    ranked = sorted(shapes.items(), key=lambda kv: kv[1]["count"], reverse=True)
    cap = limit or MAX_SHAPES_TO_LLM
    if len(ranked) <= cap:
        items = ranked
    else:
        tail_budget = max(1, cap // 5)
        head_budget = cap - tail_budget
        head = ranked[:head_budget]
        tail = ranked[-tail_budget:]
        seen_keys = {k for k, _ in head}
        items = head + [kv for kv in tail if kv[0] not in seen_keys]

    id_by_index = {}
    lines = []
    for i, (key, entry) in enumerate(items):
        sid = f"s{i}"
        id_by_index[sid] = key
        examples = entry["examples"][:2] if entry.get("examples") else []
        example_strs = [_describe_chain(ex, by_fqn) for ex in examples]
        examples_text = "; ".join(example_strs)
        lines.append(
            f"{sid}: roles=[{' -> '.join(entry['roles'])}] count={entry['count']} examples={examples_text}"
        )
    user = "shape catalogue:\n" + "\n".join(lines)
    result = llm.extract(_STAGE1_SYSTEM, user, _STAGE1_SCHEMA)
    risky_ids = result.get("risky_shape_ids", []) if isinstance(result, dict) else []
    return {id_by_index[sid] for sid in risky_ids if sid in id_by_index}


_STAGE2_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subcategory": {
                        "type": "string",
                        "description": (
                            "concise snake_case name of the architectural/design problem, e.g. "
                            "layering_violation, missing_authorization, "
                            "circular_architectural_dependency, unclear_ownership, "
                            "god_function, leaky_abstraction, missing_validation_layer. "
                            "Pick the most precise name — do NOT force it into a preset list."
                        ),
                    },
                    "owning_fqn": {
                        "type": "string",
                        "description": "which function in the given chain this finding is anchored to",
                    },
                    "message": {"type": "string"},
                    "evidence": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "severity": {
                        "type": "number",
                        "description": "how bad IF real, 0-10 (ignore reachability — scoring adds that)",
                    },
                    "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                },
                "required": ["subcategory", "owning_fqn", "message", "severity"],
            },
        }
    },
    "required": ["findings"],
}

_STAGE2_SYSTEM = (
    "You are Agent C — a software architect doing a deep review of ONE specific call chain. You "
    "are given the RAW SOURCE of every function in the chain, in order, each labeled with its role "
    "and role-confidence. Decide whether this chain has a real architecture or design problem: a "
    "skipped/backwards layer, a chain too deep or convoluted to maintain, a role repeating in a way "
    "that suggests missing abstraction, a god-function doing too much, a leaky abstraction, or (if "
    "the chain reaches something sensitive like raw SQL/shell/filesystem) a missing "
    "validation/authorization layer given the roles shown.\n\n"
    "Classification is FREE-FORM — name each finding with the most precise concise snake_case "
    "subcategory you can; do NOT force it into a preset list. Anchor every finding to the single "
    "function in the chain most responsible for it, in `owning_fqn`. Set `severity` 0-10 = how bad "
    "the design problem is if real (a missing authz layer on a sensitive op ~8, a cosmetic layering "
    "skip ~2). Do not invent behavior not shown in the given source.\n\n"
    "IMPORTANT: role labels come from a heuristic classifier, not verified ground truth — treat "
    "MEDIUM/LOW-confidence roles and `unknown`/unlabeled roles as uncertain, not as proof of a "
    "layering problem; an unlabeled step with no security-sensitive action in its source is not "
    "itself an authorization or layering issue. If a function's role is simply unclear and nothing "
    "else is wrong, use `unclear_ownership` (LOW confidence, low severity) rather than forcing it "
    "into `missing_authorization` — do not claim a missing authorization check unless the code "
    "actually shows a sensitive operation (data mutation, deletion, raw SQL/shell/filesystem "
    "access) with no visible auth/validation step anywhere in the chain. If this chain has no real "
    "problem, return an EMPTY findings array — do not invent a finding just to say something, and "
    "do not report the same underlying issue more than once across different functions in the chain."
)


def _analyze_shape_instance(store: GraphStore, by_fqn: dict, root: str, shape_key: tuple,
                             fqns: list, llm) -> list[Finding]:
    """Deep-dive ONE concrete chain instance of a flagged shape by reading the
    raw source of every function in it. Split out of `analyze_shape` so every
    example instance of a shape gets its own LLM call, not just the first."""
    chain_lines = []
    for fqn, src_block in zip(fqns, _chain_source_blocks(root, fqns, by_fqn, store=store)):
        row = by_fqn.get(fqn, {})
        role = row.get("component_role") or "unknown"
        conf = row.get("role_confidence") or "?"
        chain_lines.append(f"{src_block}\n(role={role}, role_conf={conf})")

    user = (
        f"shape: {' -> '.join(shape_key)}\n\n"
        f"chain (source -> sink), raw code:\n" + "\n\n".join(chain_lines)
    )
    result = llm.extract(_STAGE2_SYSTEM, user, _STAGE2_SCHEMA)
    items = result.get("findings", []) if isinstance(result, dict) else []
    out = []
    for item in items:
        owning_fqn = str(item.get("owning_fqn") or fqns[0])
        row = by_fqn.get(owning_fqn, {})
        f = Finding.from_dict(item, category="design", source="llm_judged",
                              owning_fqn=owning_fqn, file=row.get("file", ""))
        if f:
            if not f.line:
                f.line = row.get("start_line") or 0
            out.append(f)
    return out


def analyze_shape(store: GraphStore, repo: str, root: str, shape_key: tuple,
                  entry: dict, llm) -> list[Finding]:
    """Stage 2 for one flagged shape: deep-dive EVERY stored representative
    instance (up to MAX_EXAMPLES_PER_SHAPE), not just the first — a shape
    with 2+ examples (e.g. two different endpoints both skipping the service
    layer) previously only ever had its first example analyzed, silently
    dropping real findings anchored on the other instance(s). Findings are
    deduped by (subcategory, owning_fqn, chain_root) — chain_root (the
    example's own entry point) is included so two DIFFERENT endpoints that
    happen to share a tail function (e.g. both call the same repository
    method directly) each still get their own finding instead of the second
    endpoint's finding being silently swallowed just because it anchored on
    the same shared owning_fqn as the first endpoint's chain."""
    if llm is None or not entry.get("examples"):
        return []
    by_fqn = _all_functions_by_fqn(store, repo)
    out: list[Finding] = []
    seen: set[tuple] = set()
    for fqns in entry["examples"]:
        chain_root = fqns[0] if fqns else ""
        for f in _analyze_shape_instance(store, by_fqn, root, shape_key, fqns, llm):
            key = (f.subcategory, f.owning_fqn, chain_root)
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
    return out


def find_unreached_cycles(store: GraphStore, repo: str) -> list[Finding]:
    """Deterministic, no-LLM: detect call-graph cycles anywhere in the repo,
    not just ones reachable from an `endpoint_handler` root.

    `collect_path_shapes` only walks starting from route entry points, so a
    cycle entirely between internal functions that no controller/route ever
    calls into is otherwise invisible to both Stage 1 and
    `deterministic_findings`. One DFS pass over the WHOLE graph (white/gray/
    black coloring), O(V+E) — not per-root, so it doesn't blow up the step
    budget."""
    nodes = _all_functions(store, repo)
    adj = _adjacency(store, repo)
    color: dict[str, int] = {}
    path: list[str] = []
    on_path: set[str] = set()
    seen_keys: set[tuple] = set()
    findings: list[Finding] = []
    steps = 0

    def dfs(node_id: str) -> None:
        nonlocal steps
        if steps >= _MAX_WALK_STEPS:
            return
        steps += 1
        color[node_id] = 1
        path.append(node_id)
        on_path.add(node_id)
        for child in adj.get(node_id, [])[:_FANOUT_CAP]:
            if child not in nodes or steps >= _MAX_WALK_STEPS:
                continue
            c = color.get(child, 0)
            if c == 0:
                dfs(child)
            elif c == 1 and child in on_path:
                idx = path.index(child)
                cycle_ids = path[idx:] + [child]
                key = tuple(sorted(set(cycle_ids)))
                if key not in seen_keys:
                    seen_keys.add(key)
                    example = [nodes[n]["fqn"] for n in cycle_ids]
                    anchor = nodes[cycle_ids[-2]] if len(cycle_ids) > 1 else nodes[child]
                    findings.append(Finding(
                        category="design", subcategory="circular_architectural_dependency",
                        source="graph_proven", owning_fqn=anchor["fqn"], file=anchor.get("file", ""),
                        line=anchor.get("start_line") or 0,
                        message=f"call chain cycles back through a layer boundary (no route "
                                f"reaches this chain): {' -> '.join(example)}",
                        confidence="MEDIUM",
                    ))
        path.pop()
        on_path.discard(node_id)
        color[node_id] = 2

    try:
        for nid in nodes:
            if steps >= _MAX_WALK_STEPS:
                break
            if color.get(nid, 0) == 0:
                dfs(nid)
    except RecursionError:
        pass

    return findings


def deterministic_findings(shapes: dict, by_fqn: dict) -> list[Finding]:
    """No-LLM fallback: cycle and max-depth markers are reported directly,
    graph_proven, without any model call."""
    out: list[Finding] = []
    for key, entry in shapes.items():
        if not key:
            continue
        tail = key[-1]
        if tail not in (CYCLE_MARK, DEEP_MARK) or not entry.get("examples"):
            continue
        example = entry["examples"][0]
        anchor_fqn = example[-1] if example else ""
        row = by_fqn.get(anchor_fqn, {})
        if tail == CYCLE_MARK:
            subcat = "circular_architectural_dependency"
            msg = f"call chain cycles back through a layer boundary: {' -> '.join(example)}"
        else:
            subcat = "chain_too_deep"
            msg = f"call chain exceeds max depth without terminating: {' -> '.join(example)}"
        out.append(Finding(
            category="design", subcategory=subcat, source="graph_proven",
            owning_fqn=anchor_fqn, file=row.get("file", ""), line=row.get("start_line") or 0,
            message=msg, confidence="MEDIUM",
        ))
    return out


def run_architecture_pass(store: GraphStore, repo: str, root: str, llm=None,
                          max_depth: int = MAX_DEPTH, shape_limit: int | None = None) -> dict:
    """Full two-stage pass. With llm=None: shapes are still collected and the
    deterministic cycle/too-deep findings are still returned (dry-run safe,
    no API calls). Severity scoring is left to the caller (cli.py/frontend
    score everything together after merging Agent A + Agent B findings), not
    done inline here, to avoid double-scoring the same findings."""
    shapes = collect_path_shapes(store, repo, max_depth=max_depth)
    by_fqn = _all_functions_by_fqn(store, repo)

    findings = deterministic_findings(shapes, by_fqn)
    seen_anchors = {(f.subcategory, f.owning_fqn) for f in findings}
    for f in find_unreached_cycles(store, repo):
        if (f.subcategory, f.owning_fqn) not in seen_anchors:
            seen_anchors.add((f.subcategory, f.owning_fqn))
            findings.append(f)
    risky_keys = flag_risky_shapes(shapes, llm, by_fqn=by_fqn, limit=shape_limit)
    for key in risky_keys:
        findings.extend(analyze_shape(store, repo, root, key, shapes[key], llm))

    return {
        "shapes_total": len(shapes),
        "shapes_flagged": len(risky_keys),
        "findings": findings,
    }


# --- Agent B orchestrator ----------------------------------------------------

def run_agent_b(store: GraphStore, repo: str, root: str, llm,
                max_hops: int = 6, qualify_limit: int | None = None,
                shape_limit: int | None = None) -> dict:
    """Single entry point running all of Agent B's passes in order:
    sanitizer tagging (LLM, cached by body_hash), deterministic DFG-based
    taint composition, LLM qualify (raw-source chains), and
    architecture/layering (shape catalogue + raw-source deep-dive +
    whole-graph cycle sweep).

    Sanitizer tagging runs first so the deterministic walk can stop at
    sanitized nodes. Results are cached by body_hash — only changed
    functions cost an LLM call on subsequent runs."""
    if llm is not None:
        tag_sanitizers(store, repo, root, llm)
    det_findings = find_taint_findings(store, repo, root=root, max_hops=max_hops)
    # Free-form security discovery over reachable chains, with the deterministic
    # findings handed in as evidence (replaces the old confirm-only qualify).
    qualified = run_agent_b_security(store, repo, root, llm, max_hops=max_hops,
                                     limit=qualify_limit) if llm else []
    arch = run_architecture_pass(store, repo, root, llm, shape_limit=shape_limit)
    return {
        "det_findings": det_findings,
        "qualified": qualified,
        "arch_findings": arch["findings"],
        "shapes_total": arch["shapes_total"],
        "shapes_flagged": arch["shapes_flagged"],
    }
