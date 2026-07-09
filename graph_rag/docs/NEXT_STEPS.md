# Next Steps — Implementation Status + What To Build

**Date:** 2026-07-08  
**Validation gate:** `./.venv/bin/python validate_fixtures.py` — currently **31/31 pass**

---

## Design Principle (owner)

> Once the graph is built, minimize AST usage. The graph IS the precomputed AST.
> If something needs to be known at analysis time, it should have been stored in the graph at index time.
> The only legitimate analysis-time file reads are raw source text sent to an LLM
> (structure lives in the graph; readable prose lives in the file).

---

## What Is Done

### Phase 1 — Heuristic Downgrade ✓

| Change | File | Result |
|---|---|---|
| `strategy_hint="fuzzy_name"` on `RawRef` → `AMBIGUOUS` confidence on Edge | `models.py`, `resolver.py`, both extractors | Fuzzy edges kept but filterable |
| STRONG vs GENERIC event method split | `extractors/python.py`, `extractors/java.py` | `emit`/`publish` always non-fuzzy; `send`/`on` fuzzy unless receiver hints at real bus |
| `_looks_like_url` guard on CALLS_API | `extractors/python.py` | `session.get("cache_key")` no longer fabricates an Endpoint node |
| Event name normalization (`OrderPlaced` → `order.placed`) | `resolver.py:_normalize_event_name` | One Event node per logical event; raw string in `display_name` |
| Exact dotted-segment pkg matching | `pipeline.py:_CLASS_ROLE_RULES` | `com.acme.reports` no longer tagged `repository` |
| `helper` fallback role deleted | `pipeline.py:_classify_roles` | Functions with no classifiable owner now have empty `component_role` |

### Phase 2 — DFG ✓

| Change | File | Result |
|---|---|---|
| New `dataflow.py` | `graph_core/dataflow.py` | Index-time DFG: `ArgFlow`, `DfgSummary`, `summarize_function`, `run_dataflow` |
| DFG fields on `Node` | `models.py` | `dfg_json`, `dfg_returns_from_params`, `dfg_hash` |
| Flow arrays on `Edge` | `models.py` | `flow_from_param`, `flow_to_param`, `flow_lines`, `const_args` |
| `run_dataflow` wired into pipeline | `pipeline.py` | Runs after SCIP; replaces extractor PASSES with DFG-computed PASSES |
| PASSES emission deleted from extractors | `extractors/python.py`, `extractors/java.py` | `dataflow.py` is the only PASSES source |
| DFG fixture | `fixtures/live_test/flows.py` | `echo`, `DataStore` field write, 2-hop chain |

---

## What Works Right Now

- **`index_repo`** — full pipeline with DFG. Every Function node gets `dfg_json` written at index time. PASSES edges carry `flow_from_param`/`flow_to_param` parallel arrays.
- **`validate_fixtures.py`** — 31/31 checks pass.
- **`analyze --no-llm`** — works; reads `taint_json` via old `run_taint_pass` path.
- **`analyze` with LLM** — works; same old path.
- **Fuzzy edge filtering** — any Cypher consumer can `WHERE e.confidence <> 'AMBIGUOUS'` to exclude low-signal heuristic edges.
- **Event dedup** — `OrderPlaced` and `order_placed` collapse to the same Event node.

---

## What Doesn't Work / Is Broken

### 1. Analyzer re-walks the AST at analysis time (critical)

**Location:** `analyzer/taint.py:run_taint_pass` (lines 587–643)  
**What it does:** Parses every Python/Java source file with tree-sitter at analysis time, walks the AST, computes transfer functions, writes `taint_json` / `taint_hash` / `taint_sink_count` to Neo4j.  
**Why it's wrong:** `dfg_json` already stores identical information, computed once at index time and stored in the graph. This is double work.  
**Live correctness bug:** `SINK_PATTERNS` (which calls are sinks) is embedded in `taint.py`. Editing the sink list has zero effect on already-indexed functions because the cached `taint_json` was written with the old patterns. Re-indexing required to see any change. Phase 3 fixes this — sink classification moves to analysis time, over `dfg_json`.

### 2. Agent A fires O(functions) Neo4j queries per file

**Location:** `analyzer/agents.py:run_agent_a_chunk` (lines 261–315)  
**What it does:** For each function in a chunk, fires `_callers(store, fn_id)` (one query) then `_hop2_summary(store, caller_id)` per caller (N more queries). A file with 10 functions and 5 callers each = 10 + 50 = 60 queries for one file, before any LLM call.  
**Fix:** Two batched queries per chunk (see Phase 4.1 below).

### 3. Sink classification is frozen at index time

As described in #1: `SINK_PATTERNS` is code, not config. Adding a custom sink name requires editing Python source AND re-indexing. Phase 3 + `sinks.py` fix this.

### 4. `taint_sink_count` drives Agent A's targeted checks

**Location:** `agents.py:_function_spans` (line 91), `run_agent_a_chunk` (line 287)  
Reads `n.taint_sink_count` from Neo4j (written by `run_taint_pass`). This count is stale the moment `SINK_PATTERNS` changes. Post-Phase 3 it should be computed on the fly from `dfg_json`.

### 5. Three separate dedup systems, no unified logic

- `graph_core/findings.py:dedupe` — dedupes on `(owning_fqn, subcategory, line)`
- `graph_core/findings.py:collapse_cross_category_duplicates` — collapses security/correctness twins
- Local `seen: set[tuple]` inside `analyze_shape` (taint.py:1603)

Called from `cli.py:143` as `collapse_cross_category_duplicates(dedupe(...))`. Fragile; drift-prone; no line-bucket fuzzing; no provenance priority (graph_proven > llm_judged).

---

## Phase 3 — Agent B Re-Founded on Graph DFG

**Goal:** Delete all AST walking from the analyzer. The graph has everything. Sink classification happens at analysis time over already-stored `dfg_json`.

### 3.1 New `analyzer/sinks.py`

Extract from `taint.py` and restructure:

```python
# Structured sink records (replaces SINK_PATTERNS + _SINK_RECEIVER_HINTS)
DEFAULT_SINKS = [
    {"vuln_class": "sql_injection",
     "names": ["execute","executemany","raw","rawquery","executequery","executeupdate"],
     "receivers": None,  # None = any receiver
     "langs": ["python", "java"]},
    {"vuln_class": "ssrf",
     "names": ["get","post","put","delete","request","urlopen","fetch"],
     "receivers": ["requests","urllib","http","httpx","aiohttp","session"],
     "langs": ["python", "java"]},
    # ... rest of SINK_PATTERNS + receiver hints merged in
]

import re
SANITIZER_HINTS = re.compile(
    r"(sanitiz|escape|clean|validate|quote|encode|strip_tags|whitelist|allowlist)", re.I
)

class SinkCatalog:
    def classify(self, recv: str, name: str) -> str | None: ...

def load_sinks(repo_root: str = "") -> SinkCatalog:
    # 1. Start from DEFAULT_SINKS
    # 2. If $GRAPH_RAG_SINKS env var -> JSON file, merge (same vuln_class extends names/receivers;
    #    {"vuln_class": X, "disabled": true} removes it)
    # 3. If <repo_root>/.graphrag-sinks.json exists, merge on top
    # Returns SinkCatalog that classify() calls against the merged list
```

### 3.2 Rewrite composition to read `dfg_json`

`find_taint_findings` and `enumerate_taint_paths` currently read `row["taint_json"]` and walk `data["sinks"]` (pre-classified at index time). Change to read `row["dfg_json"]` and classify at walk time:

```python
# Old pattern in _walk_taint / _walk_taint_paths:
for sink in data.get("sinks", []):
    if param_idx not in sink.get("from_params", []): continue
    ...

# New pattern:
catalog = load_sinks(repo_root)
for af in data.get("passes", []):
    if param_idx not in af.get("from_params", []): continue
    vuln_class = catalog.classify(af.get("recv", ""), af["callee"])
    if vuln_class:
        # same sink handling as before, just the source changed
```

The walk structure (`_walk_taint`, `_walk_taint_paths`, `_resolve_callees`, `_is_sanitized`) stays identical. Only the data source and classification timing changes.

The Neo4j query in `find_taint_findings` changes:
```python
# Old:
"n.taint_json AS taint_json"
# New:
"n.dfg_json AS dfg_json"
```

### 3.3 Update `find_sanitizer_candidates` and `_own_sink_params`

Both currently parse `taint_json`. Switch to `dfg_json`:

```python
# find_sanitizer_candidates: callee_names from dfg_json.passes[].callee
# _own_sink_params: classify dfg_json.passes[] against SinkCatalog to find own sinks
```

### 3.4 Delete from `taint.py` (~400 lines)

Once 3.2 + 3.3 done, delete these blocks — they now live in `dataflow.py` or `sinks.py`:

- Lines 77–91: `_TAINT_INERT_BUILTINS` (already in `dataflow.py`)
- Lines 93–158: `SINK_PATTERNS`, `_SINK_RECEIVER_HINTS`, `_SANITIZER_NAME_HINTS`, `classify_sink` (→ `sinks.py`)
- Lines 161–240: `_callee_parts`, `_identifiers`, `_own_scope`, `_call_lhs_assign_target`, `_splat_literal_entries` (already in `dataflow.py`)
- Lines 243–426: `FunctionTaint`, `analyze_function`
- Lines 439–584: `_JAVA_SCOPE_STOP_TYPES`, `_own_scope_java`, `_java_identifiers`, `_java_callee_parts`, `_java_call_lhs_assign_target`, `analyze_function_java`
- Lines 587–643: `run_taint_pass` — the main thing to kill
- Remove imports: `from ..graph_core.languages import get_parser`, `from ..graph_core.discovery import discover`

Update module docstring: "pass 1 is now read dfg_json written at index time; sink classification happens here at walk time."

### 3.5 Agent A: derive taint flags from `dfg_json`

In `agents.py:_function_spans`, replace `n.taint_sink_count` with `n.dfg_json`:

```python
rows = store.read(
    "MATCH (n:Function {repo:$repo, file:$file}) "
    "RETURN n.id AS id, n.fqn AS fqn, n.name AS name, "
    "n.start_line AS start_line, n.end_line AS end_line, n.dfg_json AS dfg_json",
    repo=repo, file=file,
)
```

In `run_agent_a_chunk`, compute sink presence on the fly:
```python
catalog = load_sinks(root)
taint_flags = [
    f"- {sp['fqn']} reaches a known dangerous sink (taint analysis)."
    for sp in chunk
    if _has_any_sink(sp.get("dfg_json"), catalog)
]
```

### 3.6 CLI changes (`cli.py`)

Remove (line 110–113):
```python
tag_stats = taint_mod.run_taint_pass(args.path, repo, store, limit=args.limit)
```

Add DFG guard before composition:
```python
fn_count = store.read("MATCH (n:Function {repo:$repo}) RETURN count(n) AS c", repo=repo)[0]["c"]
dfg_ready = store.read("MATCH (n:Function {repo:$repo}) WHERE n.dfg_json IS NOT NULL RETURN count(n) AS c", repo=repo)[0]["c"]
if fn_count > 0 and dfg_ready == 0:
    print("ERROR: repo indexed before DFG support. Re-run: graph_rag.cli index <path> --repo <name>")
    return 2
```

Add one-time cleanup of stale `taint_json` / `taint_hash` / `taint_sink_count` props.

---

## Phase 4 — Performance + Unified Dedup

### 4.1 Batch Agent A context queries

Replace per-function queries in `agents.py` with two queries per chunk:

```python
def _batch_callers(store, ids: list[str]) -> dict[str, list[dict]]:
    rows = store.read(
        "MATCH (c:Function)-[:CALLS]->(m:Function) WHERE m.id IN $ids "
        "WITH m, c LIMIT 4000 "
        "RETURN m.id AS target, collect({"
        "id:c.id, fqn:c.fqn, name:c.name, file:c.file, "
        "signature:c.signature, docstring:c.docstring, "
        "component_role:c.component_role, role_confidence:c.role_confidence"
        "})[..40] AS callers",
        ids=ids,
    )
    return {r["target"]: r["callers"] for r in rows}

def _batch_hop2_summary(store, caller_ids: list[str]) -> dict[str, str]:
    rows = store.read(
        "MATCH (gc:Function)-[:CALLS]->(c:Function) WHERE c.id IN $ids "
        "RETURN c.id AS cid, gc.component_role AS role, count(*) AS n",
        ids=caller_ids,
    )
    grouped: dict[str, dict[str, int]] = {}
    for r in rows:
        grouped.setdefault(r["cid"], {})[r.get("role") or "unknown"] = r["n"]
    return {cid: ", ".join(f"{role}={n}" for role, n in sorted(cnt.items()))
            for cid, cnt in grouped.items()}
```

Drop `_callers` and `_hop2_summary` once callers are batched. Prompt format stays identical.

### 4.2 Concurrency

`ThreadPoolExecutor(max_workers=6)` around three loops:
- Agent A chunks in `run_agent_a_scan` (collect in input order, not completion order)
- `qualify_taint_finding` calls in `run_taint_qualify`
- `_analyze_shape_instance` calls in `analyze_shape`

Check `graph_core/llm.py` is thread-safe (per-call HTTP session) before enabling. If not, construct one LLM client per worker.

### 4.3 Source-block cache

Per-run `dict[fn_id, str]` for raw source blocks built by `_chain_source_blocks`. Both `run_taint_qualify` and `run_architecture_pass` call this; a popular function's source should be read once per run, not once per finding.

### 4.4 New `analyzer/dedup.py`

```python
FAMILY: dict[str, str] = {
    "sql_injection": "sql_injection",
    "incorrect_sql_construction": "sql_injection",   # Agent A twin
    "command_injection": "command_injection",
    # extend as cross-agent drift is found
}

def dedupe_findings(findings: list[Finding]) -> list[Finding]:
    """Unified dedup: key = (owning_fqn, family, line_bucket).
    On collision: graph_proven > llm_judged, then HIGH > MEDIUM > LOW, then first-seen."""
```

Replace in `cli.py`:
```python
# Old:
all_findings = collapse_cross_category_duplicates(dedupe(stage1 + agent_findings + arch_findings))
# New:
from .analyzer.dedup import dedupe_findings
all_findings = dedupe_findings(stage1 + agent_findings + arch_findings)
```

Remove `dedupe` and `collapse_cross_category_duplicates` from `graph_core/findings.py` (or thin-wrap to dedup.py for any existing callers).

---

## AST Usage Map — Current vs Target

| Code site | Current | Target state | Phase |
|---|---|---|---|
| `taint.py:run_taint_pass` | Parses ALL files at analysis time | **Delete** — `dfg_json` has this | 3 |
| `taint.py:find_taint_findings` | Reads `taint_json.sinks[]` (pre-classified) | Read `dfg_json.passes[]`, classify via `sinks.py` at walk time | 3 |
| `agents.py:_function_spans` | Reads `taint_sink_count` (stale index prop) | Compute from `dfg_json` + `SinkCatalog` at query time | 3 |
| `taint.py:_chain_source_blocks` | Reads raw file source for LLM | **Keep** — legitimate LLM context (prose not structure) | — |
| `agents.py:_read_source` | Reads raw file source for LLM | **Keep** — same | — |
| `context.py:file_imports_block` | Reads file for import block | Keep for now; could store `imports_summary` in File node eventually | — |
| `dataflow.py:run_dataflow` | Re-parses files at index time | Phase 5 only — pass extractor trees through | 5 |

**Rule:** If it's in a graph node/edge property, read from the graph. If it's raw source for an LLM prompt, read from file. Never parse at analysis time.

---

## File Map

```
graph_core/
  dataflow.py          ✓ NEW (Phase 2) — index-time DFG; owns PASSES edges
  models.py            ✓ MODIFIED — Edge + Node gained DFG fields
  pipeline.py          ✓ MODIFIED — run_dataflow wired in after SCIP
  findings.py          NEEDS Phase 4 — dedupe/collapse replaced by dedup.py

analyzer/
  taint.py             NEEDS Phase 3 — delete run_taint_pass, analyze_function*, ~400 lines
  sinks.py             NEEDS Phase 3 — NEW: SINK_PATTERNS + load_sinks + SinkCatalog
  agents.py            NEEDS Phase 4 — batch queries, concurrency, dfg_json taint flags
  dedup.py             NEEDS Phase 4 — NEW: unified dedupe_findings

cli.py                 NEEDS Phase 3 — remove run_taint_pass call, add DFG guard

fixtures/
  live_test/flows.py   ✓ NEW (Phase 2) — echo, DataStore, 2-hop chain
  live_test/noise.py   ✓ NEW (Phase 1) — URL guard, fuzzy event tests

docs/
  DFG_REFIT_PLAN.md   original 4-phase spec (do not edit)
  NEXT_STEPS.md        this file
```
