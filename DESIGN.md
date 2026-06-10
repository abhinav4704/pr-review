# Design & Architecture

How the system works, end to end. This document covers the **whole pipeline** —
graph construction *and* the snippet-driven PR-review retrieval layer.

For the exhaustive graph ontology (node/edge grammar, ID rules, Neo4j model,
Cypher validation queries, known failure modes), see the companion
[`architecture.md`](architecture.md). This file focuses on the moving parts and
the data flow, especially the review pipeline.

---

## 1. The big picture

```
                    ┌──────────────────────────────────────────────┐
   source code  ──▶ │  build.py  (deterministic static analysis)    │ ──▶ graph.json
                    │  cpg/backend.py · cpg/frontend.py             │     (nodes, edges,
                    │  cpg/resolve.py · cpg/store.py                │      optional source_map)
                    └──────────────────────────────────────────────┘
                                          │
                                          ▼
   raw snippet  ──▶ ┌──────────────────────────────────────────────┐
   (a PR hunk)      │  review_pr.py                                 │
                    │   1. cpg/locate.py   snippet → seed node ids  │
                    │   2. cpg/retrieve.py BFS expand + slice source│ ──▶ context bundle
                    │   3. cpg/review_llm.py  LLM review (optional) │ ──▶ review feedback
                    └──────────────────────────────────────────────┘
```

Two clean phases: **build** (offline, no LLM, deterministic) and **review** (uses
the graph as a retrieval index; LLM only at the very end, and only if asked).

---

## 2. Phase 1 — building the graph

Goal: one **multiplex property graph** (multiple edge layers over shared nodes)
describing the codebase, with stable content-addressable node IDs so re-runs merge
cleanly.

### 2.1 Pipeline (`build.py`)

1. Walk the repo with a fixed skip-list (`.git`, `node_modules`, `venv`, …).
2. For each Python file → `cpg.backend.extract_python` (per-file nodes/edges).
3. For each JS/TS file → `cpg.frontend.extract_frontend`.
4. `cpg.resolve.build` merges per-file payloads and runs whole-repo resolution.
5. `cpg.store` writes `graph.json` (and optionally loads Neo4j).

Robustness: a file that fails to parse or analyse is **skipped with a warning**;
one bad file never aborts the build.

### 2.2 Extraction (`cpg/backend.py`)

A `ast.NodeVisitor` walks each Python module and emits nodes/edges for:

- **Structure** — `File → Class → Function` via `CONTAINS`.
- **Routes** — FastAPI decorators (`@router.get("/x")`) → `Route` node + `HANDLES`.
- **Auth / deps** — `Depends(...)` → `DEPENDS_ON`; auth-looking deps also
  `GUARDED_BY` (keyword-filtered so `get_db` is not treated as auth).
- **Data access** — ORM/SQL calls → `Table` node + `READS_TABLE` / `WRITES_TABLE`
  (conservative; guards against `dict.get()` and instance args creating fake tables).
- **Concurrency** — lock factories/`with`/`acquire` → `Lock` + `ACQUIRES`.
- **Imports** — `IMPORTS` to `ExternalModule`.
- **Calls** — every call site emits an **unresolved** `CALLS` edge (resolved later).
- **Decorators / inheritance / raises / validation** — `DECORATES`, `INHERITS`,
  `RAISES`, `VALIDATES_WITH` (some as placeholders resolved later).

Crucially, **every node carries `line_start` and `line_end`** (`_end_line` uses the
AST `end_lineno`). This is what makes source retrieval possible later.

### 2.3 Resolution (`cpg/resolve.py`)

Per-file extraction is intentionally dumb; the global picture is assembled here:

- Indexes symbols across the repo (module exports, file functions, class methods).
- Resolves `CALLS` placeholders to real internal `Function` nodes (same-file,
  imported, or `self.method`); unresolved calls (stdlib/noise) are dropped.
- Re-keys placeholder edges (`GUARDED_BY`, `DEPENDS_ON`, `VALIDATES_WITH`,
  `INHERITS`, `RAISES`) to real nodes or `ExternalSymbol` stubs.
- **Router prefix pass** — accumulates `include_router(prefix=...)` chains
  transitively and rewrites `Route` paths/IDs to their full path.
- **Frontend→backend bridge** — matches `FrontendCall` nodes to `Route` nodes by
  `(method, normalized path)`, with suffix-alias matching for base-URL prefixes.

### 2.4 Persistence (`cpg/store.py`)

- `to_json` — writes `{nodes, edges}` (plus `source_map` if `--source-map`).
- `to_neo4j` — MERGEs nodes under concrete kind labels and writes statically-typed
  relationships from a fixed allowlist (never a generic `:REL`).

### 2.5 The graph shape

- **Node kinds:** `File, Class, Function, Route, FrontendCall, Table, Lock,
  ExternalModule, ExternalSymbol, Decorator, Component`.
- **Edge types:** `CONTAINS, CALLS, IMPORTS, HANDLES, CALLS_ENDPOINT,
  READS_TABLE, WRITES_TABLE, GUARDED_BY, ACQUIRES, DECORATES, DEPENDS_ON,
  VALIDATES_WITH, INHERITS, RAISES, RENDERS, USES_COMPONENT`.
- A node is a dict: `{id, kind, name, file, line, line_start, line_end, ...attrs}`.
- An edge is a dict: `{src, dst, type, ...attrs}`.

---

## 3. Phase 2 — snippet-driven PR review

This is the layer that turns the graph into a retrieval index for review. Three
steps, orchestrated by `review_pr.py`.

### 3.1 Step 1 — Locate: snippet → seed node IDs (`cpg/locate.py`)

**You never supply a node ID.** Given raw code, the locator derives the seeds.

`extract_entities(snippet)` parses the snippet with `ast` (tolerating
over-indented fragments by dedenting / wrapping in a function body) and collects:

| Candidate | From |
| --- | --- |
| `defined`  | `def` / `async def` / `class` names |
| `called`   | `name(...)` call targets |
| `attrs`    | `obj.method(...)` attribute-call tails |
| `imported` | `import` / `from … import` names |
| `routes`   | string literals that look like paths (`/x/{p}`, normalised) |

`seeds_from_snippet(snippet, nodes)` then matches those strings against node
**names** and **ID suffixes**, in priority order:

1. **defined** entities (strongest — the snippet *is* this code),
2. **called** targets,
3. **attribute-call** tails (filtered by a stoplist so `db.add()`, `payload.get()`,
   `query.all()` don't seed unrelated functions named `add`/`get`/`all`),
4. **imported** symbols,
5. **route** literals (matched against `Route.path`).

Matching is by exact `name`, by Function qualname tail (`RecordService.create` →
tail `create`), and by route path. The result is a deduplicated, priority-ordered
list of node IDs = **seeds**.

**Fallback:** if deterministic matching yields nothing and `--use-llm-entities` is
set, `cpg.review_llm.llm_pick_nodes` sends the snippet plus a compact node catalog
to the LLM, which returns IDs (validated against the catalog).

**Forward-looking:** `seeds_from_changes(file, line_ranges, nodes)` maps a changed
file + line spans to nodes by `[line_start, line_end]` overlap — the entry point
for a future real-diff workflow. It is hardened against reversed/malformed ranges.

### 3.2 Step 2 — Expand & slice (`cpg/retrieve.py`, reused as-is)

From the seeds, build the surrounding context:

- **BFS expansion** (`subgraph`) walks edges out from each seed.
- **Risk-adaptive depth** (`compute_hops`): a seed touching a high-risk edge
  (`GUARDED_BY`, `WRITES_TABLE`, `DEPENDS_ON`, `CALLS_ENDPOINT`) expands to depth
  **3**; otherwise depth **1**. Rationale: security/data-mutating code deserves more
  surrounding context. `--hops-depth N` overrides this with a fixed depth.
- **Source slicing** (`slice_node`): for each node, read lines
  `line_start..line_end` from the source map → the actual code text.
- **Token budget** (`context_for`): items are concatenated seed-first and trimmed
  tail-first to stay under `--token-limit` (≈ 4 chars/token).

Each bundle item: `{id, kind, name, file, source, hops}`.

#### Where the source text comes from

Slicing always keys off `line_start`/`line_end`; the **text** comes from a
`source_map` (`relpath → full file text`). `review_pr.py` builds it via
`locate.build_source_map`, preferring the graph's embedded `source_map`
(`build --source-map`) and otherwise reading files under `--repo`.

> **Path-separator note:** the map is keyed by the *exact* `node["file"]` string,
> because `slice_node` looks it up verbatim. Windows-built graphs store backslash
> paths (`app\main.py`); the loader matches them and only normalises separators for
> the filesystem read. (This was a real bug — keys were normalised but lookups were
> not — now fixed.)

If matched nodes exist but **no** source can be read (graph built over a different
or absent checkout, no embedded map), `review_pr.py` prints a clear warning and,
under `--llm`, **refuses** to call the model rather than send empty context.

### 3.3 Step 3 — Review (`cpg/review_llm.py`, optional)

`generate_review(snippet, bundle)` builds a prompt: a senior-reviewer system
message + the code under review + the related code (each block labelled with
`kind / name / file` and its sliced source, capped by a char safety budget), and
calls an OpenAI-compatible Chat Completions endpoint.

Both LLM functions share `_chat`, a thin `urllib` POST (no SDK dependency),
configured by `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`, with `.env`
auto-loaded when `python-dotenv` is present.

---

## 4. Why these design choices

| Choice | Reason |
| --- | --- |
| **Deterministic graph, no LLM in build** | Reproducible, auditable, cheap; the LLM is only a consumer of retrieved context. |
| **Seeds derived from code, not IDs** | The user has a PR hunk, not graph internals. AST name-matching maps code → graph automatically. |
| **AST matching first, LLM fallback** | Free, offline, deterministic for the common case; LLM only when the snippet is unparseable or anchors to nothing. |
| **Risk-adaptive hops** | Auth/data-mutation changes need broader blast-radius context than a pure refactor. |
| **Slice by `line_start`/`line_end`** | The graph already records exact spans; retrieval is a precise lookup, not a re-parse. |
| **Stoplist on attribute calls** | Prevents `db.add()` / `.get()` from dragging in unrelated same-named functions. |
| **Refuse LLM on empty context** | Avoids spending tokens on a useless prompt and surfaces misconfiguration loudly. |
| **`urllib`, not an SDK** | Zero new dependency for the core path; mirrors the existing `cypher_qa.py`. |

---

## 5. File map

| File | Responsibility |
| --- | --- |
| `build.py` | CLI: walk repo, orchestrate extraction → resolve → persist |
| `cpg/model.py` | Node/edge constructors, stable ID builders, `normalize_path` |
| `cpg/backend.py` | Python (FastAPI) static extraction via `ast` |
| `cpg/frontend.py` | JS/TS frontend-call extraction (needs Node + `typescript`) |
| `cpg/resolve.py` | Whole-repo resolution: calls, prefixes, bridges, placeholders |
| `cpg/store.py` | `to_json`, `to_neo4j`, `stats` |
| `cpg/retrieve.py` | BFS `subgraph`, `compute_hops`, `slice_node`, `context_for` |
| **`cpg/locate.py`** | **NEW** — snippet → entities → seed node IDs; source-map loader |
| **`cpg/review_llm.py`** | **NEW** — LLM entity-pick fallback + review generation |
| **`review_pr.py`** | **NEW** — CLI orchestrating locate → expand/slice → review |
| `ask_graph.py` | Ad-hoc graph queries: offline hop traversal or LLM→Cypher |
| `cpg/cypher_qa.py` | NL question → read-only Cypher over Neo4j |
| `architecture.md` | Authoritative graph ontology / Neo4j spec |

---

## 6. Data contracts

**Graph file (`graph.json`):**
```json
{
  "nodes": [{"id": "...", "kind": "Function", "name": "...", "file": "...",
             "line_start": 12, "line_end": 40, "...": "..."}],
  "edges": [{"src": "...", "dst": "...", "type": "CALLS", "...": "..."}],
  "source_map": {"relpath": "full file text"}        // only with --source-map
}
```

**Review bundle (per item):**
```json
{"id": "...", "kind": "Function", "name": "...", "file": "...",
 "source": "<sliced code>", "hops": 1}
```

---

## 7. Extending it

- **Real PR diffs:** parse a unified diff into `(file, [(start,end), …])` and feed
  `locate.seeds_from_changes` (already implemented and hardened) instead of, or in
  addition to, `seeds_from_snippet`. Build the graph on the reviewed commit so diff
  line numbers align with `line_start`/`line_end`.
- **Different retrieval policy:** `context_for(..., policy=...)` is reserved for new
  strategies (e.g. shallow/deep) beyond the current `"risk"` policy.
- **Different model/provider:** point `OPENAI_BASE_URL`/`OPENAI_MODEL` at any
  OpenAI-compatible endpoint; the transport is provider-agnostic HTTP.
- **Edge-weighted expansion:** `subgraph(..., edge_filter=)` accepts a predicate to
  restrict traversal to chosen relationship types.
