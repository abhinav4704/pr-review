# Codebase Brain — STATUS (current architecture, as-built)

> What exists **right now**. For the full vision/roadmap see [`ARCHITECTURE.md`](ARCHITECTURE.md).

**Goal:** a multi-layer Neo4j knowledge graph over legacy Java/Python that will power an
agentic code generator grounded strictly in the existing code. Thesis: *index once, query
cheap* — GraphRAG over code as an alternative to fine-tuning, with verifiable grounding.

**What's built today:** a deterministic (no-LLM) pipeline that ingests a Java/Python repo
and materializes it as a confidence-tagged, provenance-tracked **structural knowledge graph
in Neo4j**. One command in, a queryable graph out. This is the compiler-grade structural
foundation. On top of it, the **semantic + vector layers are built** (`semantic.py`,
`embeddings.py`); **hybrid retrieval and the agent are not yet**.

---

## Where we are (2026-06-30)

| Phase | State |
|---|---|
| **1 — Structural graph** | ✅ Built + tested (deterministic; events/auth/roles/modules/USES; REFERENCES reachable; `validate_fixtures.py` = 19/19) |
| **2 — Semantic** | ✅ **Code complete, unverified live.** Identity (+`keywords`/`tags`/`concepts`) for every node, typed Implementation Flow per function. Cached by `body_hash` + schema version. |
| **3 — Embeddings (vector leg)** | ✅ **Code complete, unverified live.** `cli embed` → identity-doc vector on each node + `code_embedding` cosine index. Local sentence-transformers default. |
| **3 — Hybrid retrieval** | ⬜ Not built. All three legs are *available* (vector index, keyword fields, graph), but the retriever + context-pack loop is not written. |
| **4–5 — Context builder / agent** | ⬜ Not built. |

> **Honesty note:** Phases 2–3 are written, imported, and wired, but have **not** been
> run end-to-end against a live Neo4j + embedding model + API key. Treat them as
> "should work" until a real `index → semantic → embed` run confirms it. Next-step
> ideas and known gaps live in [`IMPROVEMENTS.md`](IMPROVEMENTS.md).

---

## Pipeline

```
discover ─► extract (tree-sitter) ─► resolve (SCIP + heuristic) ─► derive ─► write → Neo4j
              per file:                  RawRefs → edges            overrides
              Nodes + CONTAINS           SCIP: precise Python       package tree
              + RawRefs                  heuristic: name+scope      call metrics
```

### Package map (`graph_rag/`)
```
cli.py            entry: index | semantic | embed  (each --repo NAME, see RUN.md)
pipeline.py       orchestrator: discover→extract→resolve→scip→overrides→packages→roles→modules/USES→metrics→validate→write
discovery.py      Stage 0: walk repo, detect lang (.java/.py), hash → FileInfo
languages.py      tree-sitter parser loading (Java + Python)
extractors/
  __init__.py     dispatch by language
  common.py       shared tree-sitter helpers
  python.py       Stage 1 Python → nodes(+metadata) + CONTAINS + RawRefs + Endpoints/CALLS_API
  java.py         Stage 1 Java   → same (+ Spring endpoints)
resolver.py       Stage 2 HEURISTIC: name+scope resolution, CALLS_API matching, coverage metric
scip_resolver.py  Stage 2 PRECISE: scip-python(Pyright) → EXTRACTED Python CALLS + OVERRIDES
scip/             vendored SCIP protobuf (scip.proto + scip_pb2.py)
apispec.py        HTTP-API helpers: route normalization, URL parsing, endpoint matching
canonical_ir.py   M6 seam: normalize/dedup extractor output into one bundle (lossless today)
validator.py      M7: graph invariants (dangling edges, dup ids, ranges, required relations)
models.py         data model: Node, Edge, RawRef, Confidence, Origin, _clean()
ids.py            stable id = sha1(repo+kind+fqn)[:16]; body_hash
schema.py         allowlists for labels/edge-types (Cypher-injection guard)
store.py          Neo4j writer: bootstrap, repo-scoped wipe, batched MERGE upserts,
                  write_semantics (patch props), create_vector_index (cosine)
config.py         Neo4j connection + scip-python binary location (Windows .cmd/.ps1 aware)
semantic.py       Phase 2: LLM Identity (+keywords/tags/concepts) per node + typed
                  Implementation Flow per function; cached by body_hash + version
llm.py            Phase 2: provider-agnostic structured-output wrapper (anthropic/bedrock/openai)
embeddings.py     Phase 3: embed the identity doc → vector on node + Neo4j vector index
                  (local sentence-transformers default; voyage/openai optional)
measure_coverage.py   dev probe (no DB): heuristic coverage + old/new lift
validate_fixtures.py  semantic/architecture edge regression (no DB): 19 checks
scip_check.py     standalone SCIP smoke test (locate → --version → index a throwaway project)
webapp/           deterministic browser UI (FastAPI + 1 static page)
samples/          test fixtures (Sample.java, Shapes.java, api_sample.py)
Dockerfile / docker-compose.yml   containerized indexer + Neo4j (see "Run in Docker")
```

---

## Graph schema (what's actually in Neo4j)

Every node also carries the shared label **`:CodeNode`** (owns the unique `id` index).
Stable **`id = sha1(repo + kind + fqn)[:16]`** — *not* line-based, so editing a body changes
`body_hash` but keeps `id` (incremental re-index patches in place). `repo` is on every node →
**multiple repos coexist**; `wipe` is repo-scoped.

### Node labels (9) — `schema.py`
`Repository` · `Package` · `File` · `Module`* · `Class` · `Function` · `Field` ·
`Annotation` · `Endpoint`   *(`Module` allowlisted, not yet emitted)*

### Edge types (18) — `schema.py`
| Family | Edges |
|---|---|
| Structure | `CONTAINS`, `IMPORTS` |
| References | `CALLS`, `INSTANTIATES`, `EXTENDS`, `IMPLEMENTS`, `ANNOTATED_WITH` |
| Types | `RETURNS`, `OF_TYPE`, `HAS_TYPE`, `HAS_GENERIC` |
| Relationships | `OVERRIDES`, `READS`, `WRITES`, `THROWS`, `CATCHES` |
| HTTP-API | `EXPOSES`, `CALLS_API` |

```
Repository ─CONTAINS→ Package ─CONTAINS→ Package ─CONTAINS→ File ─CONTAINS→ Class/Function/Field
Class ─EXTENDS/IMPLEMENTS→ Class            Function ─CALLS→ Function (EXTRACTED via SCIP, Py)
Function ─OVERRIDES→ Function               Function ─RETURNS/HAS_TYPE/HAS_GENERIC→ Class
Function ─READS/WRITES→ Field               Field ─OF_TYPE→ Class
Function ─THROWS/CATCHES→ Class             Class/Function ─ANNOTATED_WITH→ Annotation
Function ─EXPOSES→ Endpoint (serves route) Function ─CALLS_API→ Endpoint (outbound HTTP)
```

### Node metadata
Source range (line+col), `display_name`, `visibility`, `modifiers`, `is_static/abstract/async`,
`return_type`, **`param_count` + `param_names` + `param_types`** (ordered, aligned input
params), `signature`, `docstring`, `body_hash`, `package` (File→its package), `extractor`,
`last_indexed`, and **static metrics** (`loc`, `cyclomatic`, `branch_count`, `loop_count`,
`fan_in`, `fan_out`, `recursive`). Endpoint nodes carry `method`, `route`, `host`.

### Edge metadata
`confidence` (EXTRACTED|INFERRED|AMBIGUOUS) · `origin` (EXTRACTED|DERIVED) · `extractor`
(tree-sitter|scip-python|heuristic|structure) · `strategy` (which resolver rule fired) ·
evidence `file:line:col`.

**Two quality axes:** `confidence` = how sure the *resolution* is; `origin` = how the fact
*entered* (read from AST/index vs derived by later analysis).

---

## How resolution works (Stage 2, two tiers)

- **SCIP / Pyright (precise, EXTRACTED)** — Python `CALLS` + `OVERRIDES`. scip-python indexes
  the repo (`cwd = repo root`), maps each symbol to our node **by definition location**, and a
  non-definition occurrence of a function symbol = a call. Measured 84.8% precision / 92.5%
  recall vs the heuristic it replaced; on primitive-pr 415 EXTRACTED CALLS + 4 OVERRIDES.
- **Heuristic (name + lexical scope, INFERRED/AMBIGUOUS)** — the fallback for Java (scip-java
  needs Maven/Gradle, not installed) and for `--no-scip`. Scope-aware CALLS cascade
  (same-scope → same-file → imports → receiver-type → arity); ~99% of in-repo Python call-sites
  scored. Type/state/exception/override edges resolve here too.

When SCIP is available for Python, the pipeline **swaps out** heuristic Python CALLS and drops
heuristic Python OVERRIDES (SCIP's are precise); everything else stays heuristic.

### OVERRIDES (Java + Python)
`_derive_overrides` (pipeline) walks the resolved class hierarchy (`EXTENDS`/`IMPLEMENTS`) and
emits OVERRIDES when a method matches an ancestor method by **name + arity** (transitive BFS,
covers interfaces). INFERRED confidence. For Python, SCIP's precise overrides win when
available; the heuristic is the only path for Java and `--no-scip`.

### HTTP-API layer (`apispec.py` + extractors + resolver)
- **`Endpoint`** node = an HTTP endpoint (`method` + normalized `route`, `host` for external).
- **`EXPOSES`** (Function → Endpoint): backend handlers, from FastAPI/Flask decorators and Java
  Spring mappings (with class-level `@RequestMapping` prefix).
- **`CALLS_API`** (Function → Endpoint): outbound HTTP calls — Python `requests`/`httpx`/`urlopen`,
  Java `RestTemplate`. The resolver matches each call's URL to an exposed endpoint (exact, then
  segment-wise template match so `/api/users/42` resolves to `/api/users/{id}`); an external host
  becomes an `external` Endpoint node; an unmatched relative path becomes an `api_unresolved`
  endpoint (surfaces a dead/missing route). Query the chain:
  `(caller)-[:CALLS_API]->(:Endpoint)<-[:EXPOSES]-(handler)`.
- **Producer-agnostic:** a future JS/TS extractor just emits the same `CALLS_API` refs. The
  frontend(React)→backend leg is **not built** — no JS grammar installed.

---

## SCIP subsystem (precise Python resolver)

Binary under `scip_tooling/node_modules` (gitignored), located by `config.scip_python_bin()`
(env `SCIP_PYTHON_BIN` → packaged → PATH). Graceful fallback to heuristic on any failure.

**Three failure modes found & fixed:**
1. **Install behind a TLS proxy (Zscaler)** — `npm install` is blocked. *Running* scip-python
   needs no network; install once / pin `0.6.6` / vendor the binary, or set npm's `cafile`.
2. **Windows lookup** — npm makes `scip-python.cmd`/`.ps1`, not an extension-less file;
   `config.py` now probes those (`.cmd` before `.ps1`) so it isn't silently skipped.
3. **Non-git directory** — scip-python defaults its version to `git rev-parse` and crashes;
   `scip_resolver.py` now passes `--project-version 0.0.0`, so vendored/extracted trees work.

`READS` are available from SCIP roles but this build emits **no WriteAccess** → state WRITES
still come from the AST. **scip-java is blocked** (needs a JVM build tool). Use `scip_check.py`
to verify a machine's SCIP install end-to-end.

---

## Run

### Local
```bash
cd graph_rag
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
npm install --prefix scip_tooling @sourcegraph/scip-python@0.6.6   # precise Python (optional)

# Neo4j (Docker)
docker run -d --name cbrain-neo4j -p7474:7474 -p7687:7687 \
  -e NEO4J_AUTH=neo4j/testpassword neo4j:5

# index
NEO4J_PASSWORD=testpassword ./.venv/bin/python -m graph_rag.cli index <path> --repo NAME
#   --no-wipe (append) · --no-scip (heuristic only) · --validation-report FILE · --fail-on-validation-error

# web UI · resolver probe (no DB) · SCIP smoke test
NEO4J_PASSWORD=testpassword ./.venv/bin/python -m uvicorn webapp.server:app --port 8000
./.venv/bin/python measure_coverage.py <path> --repo NAME
./.venv/bin/python scip_check.py
```

### Docker (containerized indexer + Neo4j)
Deps are baked into the image (`Dockerfile`) so a run never hits a package registry — the
fragile per-run install is gone. Set the repo to index in `.env` (`REPO_PATH`, host path), then:
```bash
docker compose build          # one-time; needs network for apt/PyPI/npm (configure proxy if behind Zscaler)
docker compose up neo4j -d
docker compose run --rm indexer
```
`REPO_PATH` is cross-platform (`../sail` on macOS/Linux, `D:/Downloads/sail` on Windows). The
target need not be a git repo (the `--project-version` fix). scip-python is pinned to `0.6.6`.

Config via env: `NEO4J_URI/USER/PASSWORD/DATABASE`, `SCIP_PYTHON_BIN`. Browse Neo4j at :7474.

---

## Example queries
```cypher
// blast radius — transitive callers
MATCH (c)-[:CALLS*1..3]->(f:Function {name:'placeOrder'}) RETURN DISTINCT c.fqn;
// state impact — who writes a field
MATCH (fn)-[r:READS|WRITES]->(f:Field {name:'balance'}) RETURN fn.fqn, type(r);
// who overrides what
MATCH (sub)-[:OVERRIDES]->(base) RETURN sub.fqn, base.fqn;
// frontend/service → endpoint → handler
MATCH (caller)-[:CALLS_API]->(e:Endpoint)<-[:EXPOSES]-(handler)
RETURN caller.fqn, e.name, handler.fqn;
// architecture — packages of a repo
MATCH (r:Repository {repo:'sail'})-[:CONTAINS*]->(p:Package) RETURN p.fqn;
```

---

## What is NOT built yet (the honest boundary)
- **Java CALLS** still heuristic (scip-java needs Maven/Gradle).
- **No JS/TS** extractor → the frontend→backend `CALLS_API` leg is unproduced.
- **Semantic layer built, but downstream of it is not:** Identity + Implementation Flow
  generation exist (`semantic.py`); still missing are **embeddings / vector index**,
  hybrid retrieval, context packs, generation (Job 2), incremental re-index, and export.
- **No CFG/DFG/PDG** (Phase 6, off the critical path) and **canonical IR is a seam, not a full
  language-neutral model** yet.
- **`Module` nodes** allowlisted but not emitted (would come from build files: pom/package.json/…).

## Done so far (milestones)
M1 Metadata & provenance ✅ · M2 Type system ✅ · M3 Symbol resolution (Python via SCIP; Java
blocked) ✅ · M4 Program relationships (READS/WRITES/THROWS/CATCHES) ✅ · M5 Static metrics ✅ ·
OVERRIDES (Java+Python, deterministic) ✅ · HTTP-API layer (Endpoint/EXPOSES/CALLS_API) ✅ ·
Package/Repository hierarchy ✅ · Param metadata on Function ✅ · M7 Validation suite (baseline) ✅.
Next per roadmap: Module nodes (from build files), then the semantic→retrieval→agent phases.

## Stale-state notes
- Docker container `cbrain-neo4j` may be stopped after reboot: `docker start cbrain-neo4j`.
- Re-indexing a repo wipes its nodes first (use `--no-wipe` to append).
