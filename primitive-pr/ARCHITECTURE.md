# Architecture — `primitive-pr` PR Review System

## Overview

A GitHub PR review pipeline that:
1. Downloads the repository source tarball via GitHub API
2. Builds a code graph (AST + call/import edges) and pushes it to Neo4j
3. Parses the PR diff and maps changed lines to graph nodes
4. Runs two concurrent review tracks (per-file + impact-chain) against an LLM (AWS Nova or OpenAI)
5. Exposes results through a Streamlit UI (`graph_explorer.py`)

---

## Module Dependency Map

```
findings.py         ← no internal deps (self-contained data model)
graph.py            ← no internal deps (tree-sitter AST → CodeGraph)
diff.py             ← graph.py (maps diff hunks → graph nodes)
neo4j_store.py      ← graph.py (pushes CodeGraph to Neo4j; architecture-digest queries)
impact.py           ← diff.py, graph.py (reverse-BFS propagation chains)
architecture.py     ← neo4j_store.py (structural digest for LLM context)
llm.py              ← boto3, openai (Bedrock converse API wrapper)
review_llm.py       ← llm.py (completion factory + single-shot helper)
pr_passes.py        ← diff.py, findings.py, impact.py, review_llm.py, neo4j_store.py
graph_explorer.py   ← everything above (Streamlit UI entry-point)
```

No circular dependencies exist. The layering is mostly clean with the violations documented below.

---

## Data Flow

```
GitHub API
    │  tarball (source tree)
    ▼
graph.py ──build_graph()──► CodeGraph (nodes + 7 edge types)
    │                            │
    │                            ▼
    │                     neo4j_store.py ──push()──► Neo4j
    │
    │  PR diff (unified-diff text)
    ▼
diff.py ──parse_diff()──► [FileDiff]
    │
    ▼
impact.py ──analyze_impact()──► [Chain]  (reverse-BFS from changed nodes)
    │
    ├──► pr_passes.py ── review_pr()        (Track A: per-file, sequential)
    │                 └─ review_pr_impact() (Track B: per-cluster, concurrent)
    │                         │
    │                         ▼
    │                   review_llm.py → llm.py → LLM
    │                         │
    │                         ▼
    │                   findings.py ── parse_findings() → [Finding]
    │                         │
    │                         ▼
    │                   verify_findings() → pruned [Finding]
    │
    ▼
graph_explorer.py ── Streamlit render
```

---

## Component Descriptions

### `findings.py`
- `Finding` dataclass: `category`, `severity`, `file`, `line`, `title`, `explanation`, `evidence`, `recommendation`
- `parse_findings(text)` — strips markdown fences, falls back to bracket scan for resilient JSON extraction
- `dedupe()` — groups by `(file, line//5, normalized_title)` — 5-line bucket deduplication
- `sort_by_severity()`, `severity_counts()` — output formatting helpers

### `graph.py`
- Two language extractors: `_PyExtractor` (tree-sitter, resolves FastAPI `Depends()` and `from x import` bindings) and `_GenericExtractor` (JS/Java/Go)
- AST fallback (`_ast_fallback`) when tree-sitter is unavailable
- 7 edge types: `defines`, `calls`, `imports`, `inherits`, `overrides`, `decorates`, `instantiates`
- Post-build resolution passes: `_resolve()` (placeholder → real node), `_resolve_overrides()` (subclass method matching)
- `build_graph(root)` — filesystem walk → extract → resolve

### `diff.py`
- `parse_diff(text)` — tracks **added lines only** with new-side line numbers; context lines advance counter
- `FileDiff.changed_lines` is an alias for `added_lines` (same set object)
- `map_changes(file_diffs, graph)` — classifies each changed node: `added | signature | behavior`
- `old_signature_for()` — best-effort scan of removed lines for the function's declaration

### `neo4j_store.py`
- `from_env()` — returns `None` if env vars unset (graceful no-op mode)
- `push()` — batched MERGE upserts (500 nodes/edges per batch), scoped by `pr_ref`
- `neighbors()` — depth 1–5, edge type and direction validated against allowlists before Cypher interpolation
- Architecture-digest queries: `kind_counts`, `module_edges`, `module_sizes`, `top_fan_in`, `top_fan_out`, `nodes_at_lines`

### `impact.py`
- `build_change_clusters()` — connected components among changed nodes (call/inherit edges only)
- `impact_tree()` — multi-source reverse BFS; stops at first unchanged "consumer" node
- `_score_chain()` — numeric relevance: signature change (+3), field hits (+4), sensitive names (+2), entrypoint (+3), distance bonus, test node (−2), uncertain edge (−2), already-modified consumer (−5)
- `gone_fields()` / `field_delta()` — regex scan of diff for dict keys removed and not re-added

### `architecture.py`
- `build_digest(store, pr_ref)` — pulls 9 structural fact-sets from Neo4j
- `detect_module_cycles()` — NetworkX DiGraph of top-level module edges → `nx.simple_cycles()`
- `digest_to_markdown()` — compact markdown context block sent to LLM

### `llm.py`
- `NovaClient` — AWS Bedrock `converse` API; botocore adaptive retries (max 6)
- `converse_with_tools()` + `normalize_blocks()` — agentic tool-loop (unused in current review passes)
- `_extract_json()` — fence-strip → direct parse → bracket-scan fallback

### `review_llm.py`
- `make_completion_fn(provider, ...)` — builds client once, returns `Callable[[system, user], str]`
- `run_completion(provider, ...)` — single-shot version (used for architecture review)

### `pr_passes.py`
- **Track A** `review_pr()` — per-file whole-file review, sequential
- **Track B** `review_pr_impact()` — per-cluster impact review, concurrent via `ThreadPoolExecutor`
- `verify_findings()` — second strict LLM pass; never wipes (empty/garbage response → keep originals)
- `_cluster_dossier()` — multi-section text: changed source (with `>` markers) → propagation chains → unchanged consumer sources
- `MAX_CONSUMERS_IN_DOSSIER = 6` caps consumers per cluster

### `graph_explorer.py`
- Streamlit entry-point; sidebar for credentials; main flow: repo select → PR select → Analyze
- Runs Track A + Track B concurrently; renders impact chains first, then per-file report
- Optional expanders: diff viewer, changed nodes, LLM prompts, graph explorer, architecture review

---

## Known Architectural Problems

### 🔴 Critical

#### 1. Diff Parsing Duplicated Between `graph_explorer.py` and `diff.py`
`graph_explorer.py` contains a local `parse_diff_files()` that re-implements unified-diff parsing.  
It diverges from `pr_review/diff.py:parse_diff()` (tracks only `path` + raw lines, not added-line numbers).  
**Risk**: The two implementations will silently diverge further as the codebase evolves.  
**Fix**: `graph_explorer.py` should call `pr_review.diff.parse_diff()` and extract paths from the returned `FileDiff` list.

#### 2. `read_source()` Duplicated Between `graph_explorer.py` and `pr_passes.py`
Both define `read_source(src_path, file_path) → str`. The `graph_explorer` variant returns `"# source not found: ..."` on missing file; `pr_passes` returns `""`.  
**Risk**: Silent divergence; missing-file handling inconsistency.  
**Fix**: Promote to `pr_review/diff.py` or a `pr_review/utils.py`, import everywhere.

---

### 🟠 High

#### 3. `review_llm.py` — Copy-Pasted Client Instantiation
`make_completion_fn()` and `run_completion()` contain **identical Nova and OpenAI instantiation blocks**.  
**Fix**: `run_completion` should delegate: `return make_completion_fn(provider, ...)(system, user)`.

#### 4. Neo4j Driver Reopened on Every Streamlit Rerender
`render_changed_nodes()`, `render_prompts()`, and `render_explorer()` each call `get_store()` independently, opening and closing a new Bolt driver per UI interaction.  
**Risk**: Connection pool exhaustion under frequent rerenders; latency spikes.  
**Fix**: Cache the `Neo4jStore` instance in Streamlit session state; close only on session end or credential change.

#### 5. `FileDiff.changed_lines` Is a Misleading Alias
`__post_init__` sets `self.changed_lines = self.added_lines` — they are the **same set object**.  
Removed lines are not tracked at all. The name `changed_lines` implies bidirectional change tracking.  
**Risk**: Callers expecting `changed_lines` to cover deleted lines will silently miss them.  
**Fix**: Remove the alias; rename to `added_lines` everywhere, or add true `removed_lines` tracking.

#### 6. `graph_explorer.py` Bypasses `pr_passes.py` for Neo4j and Diff Logic
The UI layer directly instantiates `Neo4jStore`, calls `map_changes()`, builds `architecture.build_digest()`, etc., rather than going through a single facade in `pr_passes.py`.  
**Risk**: UI becomes tightly coupled to internals; changes to Neo4j schema or diff logic must be updated in two places.  
**Fix**: Expose a `run_full_review(repo, pr_number, src_path, ...) -> ReviewResult` function in `pr_passes.py` that encapsulates the full pipeline.

---

### 🟡 Medium

#### 7. `_extract_json_array()` (findings.py) and `_extract_json()` (llm.py) — Parallel Implementations
Both strip markdown fences and fall back to bracket scanning. `findings.py` is intentionally self-contained, but the duplication means fence-stripping bugs must be fixed in two places.  
**Fix**: Extract to `pr_review/utils.py:extract_json_block(text, kind="array"|"object")`.

#### 8. No Source Tarball Caching
Every "Analyze PR" click re-downloads and re-extracts the full repository tarball.  
**Risk**: Rate-limit consumption; slow UX on large repos.  
**Fix**: Cache by `(repo, sha)` in a temp directory with a TTL; reuse across rerenders.

#### 9. Track A is Sequential; Track B is Concurrent — No Coordination
Track A (`review_pr`) iterates files sequentially. Track B (`review_pr_impact`) uses `ThreadPoolExecutor`. Both are launched concurrently from `graph_explorer.py` with `concurrent.futures`, but their LLM calls share no rate-limit budget.  
**Risk**: Burst of parallel LLM requests during Track B can hit provider rate limits while Track A is also running.  
**Fix**: Shared semaphore or token-bucket rate limiter passed into both tracks.

#### 10. `MAX_CONSUMERS_IN_DOSSIER = 6` Is a Hardcoded Magic Number
No explanation in the code for why 6.  
**Fix**: Move to a named config constant with a docstring, or make it a parameter of `review_pr_impact()`.

---

### 🔵 Low

#### 11. `__init__.py` Exports Nothing
The package `__init__.py` is empty. External consumers must know every submodule path.  
**Fix**: Re-export the primary public API (`build_graph`, `parse_diff`, `analyze_impact`, `review_pr`, `review_pr_impact`).

#### 12. `diagnose_neo4j.py` Silent Empty-Password Fallback
`pwd = os.environ.get("NEO4J_PASSWORD", "")` — missing env var produces an undiagnosed connection failure.  
**Fix**: Replace with `os.environ["NEO4J_PASSWORD"]` (raises `KeyError`) or log a clear warning before attempting connection.

#### 13. `graph_explorer.py` Imports `parse_diff` But Uses Local `parse_diff_files`
The import `from pr_review.diff import parse_diff` exists at the top of `graph_explorer.py` but the local `parse_diff_files` is what actually gets called. The import is dead code.  
**Fix**: Delete the local function; use the imported `parse_diff`.

---

## Correct Dependency Layering (Target State)

```
┌─────────────────────────────────────────────┐
│  graph_explorer.py  (UI / Streamlit)        │  ← calls only pr_passes + review_llm
└────────────────┬────────────────────────────┘
                 │
┌────────────────▼────────────────────────────┐
│  pr_passes.py  (Orchestration facade)       │
└──┬──────┬──────┬───────────┬────────────────┘
   │      │      │           │
   ▼      ▼      ▼           ▼
diff  findings  impact  review_llm → llm
   │                        
   ▼                        
 graph → neo4j_store        
                │            
                ▼            
           architecture      
```

---

## Security Notes

| Item | Status |
|------|--------|
| Cypher `rel_type` interpolation | ✅ Safe — validated against closed allowlist before use |
| Tarball path traversal | ✅ Guarded for Python < 3.12 in `github_client.py` |
| TLS bypass (`PR_REVIEW_INSECURE_TLS`) | ⚠️ Escape hatch present; logs a warning but should be blocked in production |
| Credentials in `.env` | ✅ `.gitignore` excludes `.env`; `.env.example` has no real secrets |
| GitHub token validation | ✅ `GitHubClient.__init__` rejects empty token |
| Neo4j password fallback | ⚠️ `diagnose_neo4j.py` silently uses empty string if env var unset |

---

## File Index

| File | Role | LOC (approx) |
|------|------|------|
| `pr_review/__init__.py` | Package marker (empty) | 0 |
| `pr_review/findings.py` | Finding data model + JSON parsing | ~130 |
| `pr_review/diff.py` | Unified-diff parser + graph mapping | ~200 |
| `pr_review/graph.py` | AST extraction + CodeGraph builder | ~500 |
| `pr_review/neo4j_store.py` | Neo4j persistence + digest queries | ~350 |
| `pr_review/impact.py` | Reverse-BFS propagation chains | ~300 |
| `pr_review/architecture.py` | Structural digest for LLM context | ~150 |
| `pr_review/llm.py` | AWS Nova / OpenAI API wrappers | ~200 |
| `pr_review/review_llm.py` | Completion factory + single-shot | ~80 |
| `pr_review/pr_passes.py` | Review orchestration (Track A + B) | ~250 |
| `pr_review/github_client.py` | GitHub REST API client | ~150 |
| `graph_explorer.py` | Streamlit UI entry-point | ~650 |
| `diagnose_neo4j.py` | Standalone Neo4j connectivity check | ~50 |
| `requirements.txt` | Python dependencies | 13 lines |
