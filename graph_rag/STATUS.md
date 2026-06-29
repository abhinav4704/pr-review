# Codebase Brain — STATUS / Handoff

> Resume file. Safe to clear the conversation after reading this.
> Design reference: [`../ARCHITECTURE.md`](../ARCHITECTURE.md). How-to-run: [`README.md`](README.md).

**Goal:** multi-layer Neo4j knowledge graph over legacy Java/Python → powers an
agentic code generator grounded strictly in the existing code. Thesis: index once,
query cheap; GraphRAG as an alternative to fine-tuning, with verifiable grounding.

---

## 🏗 CURRENT ARCHITECTURE (as-built — everything we have right now)

A pipeline that ingests a Java/Python repo and materializes it as a confidence-tagged,
provenance-tracked **structural knowledge graph in Neo4j**. One command in, a queryable
graph out (~3s on `primitive-pr`). This is the compiler-grade structural foundation;
no semantic/LLM layer yet.

### Package map (`graph_rag/`)
```
cli.py            entry: `index <path> --repo NAME [--no-wipe] [--no-scip]`
pipeline.py       orchestrator: index_repo() — discover → extract → resolve → write
discovery.py      Stage 0: walk repo, detect lang (.java/.py), hash → FileInfo
languages.py      tree-sitter parser loading (Java + Python)
extractors/
  __init__.py     dispatch by language
  common.py       shared TS helpers (text, iter_descendants, simple_type_name)
  python.py       Stage 1 Python → nodes(+metadata) + CONTAINS(+provenance) + RawRefs
  java.py         Stage 1 Java   → same
resolver.py       Stage 2 HEURISTIC: name+scope resolution, Coverage metric
scip_resolver.py  Stage 2 PRECISE: scip-python(Pyright) → EXTRACTED Python CALLS
scip/             vendored SCIP protobuf (scip.proto + scip_pb2.py) + ingestion
models.py         data model: Node, Edge, RawRef, Confidence, Origin, _clean()
ids.py            stable id = sha1(repo+kind+fqn)[:16]; body_hash
schema.py         allowlists for labels/edge-types (Cypher-injection guard)
store.py          Neo4j writer: bootstrap, repo-scoped wipe, batched MERGE upserts
config.py         Neo4j connection + scip-python binary location
measure_coverage.py   dev probe (no DB): heuristic coverage + old/new lift
webapp/           deterministic browser UI (FastAPI + 1 static page) — see webapp/README.md
                    add repo (github/zip) · search fn/class · deps (in/out) · source code
```
**Web UI** (`webapp/`, no LLM): `uvicorn webapp.server:app` → http://localhost:8000.
Add a repo (GitHub clone or `.zip`) → indexes it; browse functions/classes, their
dependencies (with confidence + provenance), and real source. Repo→disk-path registry in
`webapp/repos/registry.json` (primitive-pr auto-seeded).

### Data flow
```
repo ─► discover ─► extract (per file) ─► resolve ───────────────► write ─► Neo4j
          │            │                    ├ HEURISTIC  → INFERRED/AMBIGUOUS edges
       FileInfo[]   Node[] (+metadata)      │   (all non-CALLS, + Java CALLS)
       (lang,sha,   Edge[] CONTAINS(+prov)  └ SCIP       → EXTRACTED Python CALLS
        bytes)      RawRef[] (name refs,        (swaps out heuristic Python CALLS)
                     +ref location)
```
Three in-memory structures are the contract between stages (`models.py`):
- **Node** — a resolved graph node.  **Edge** — a resolved relationship.
- **RawRef** — a reference whose target is only known *by name* (extractors emit these;
  the resolver turns them into Edges). Carries the reference-site location for provenance.

### Pipeline stages
- **0 Discover** — walk (skip .git/.venv/node_modules/build/…), lang by extension, hash.
- **1 Extract (tree-sitter)** — per file: emit File/Class/Function/Field nodes with full
  metadata; emit resolved **CONTAINS** edges; emit **RawRefs** for everything cross-symbol
  (CALLS/IMPORTS/EXTENDS/IMPLEMENTS/INSTANTIATES/ANNOTATED_WITH). Python also records the
  call **receiver** on each CALLS ref.
- **2 Resolve (two tiers)** — turn RawRefs into edges:
  - *Heuristic* (`resolver.py`): no types; resolve by name + lexical scope. CALLS cascade:
    `self.x()`→enclosing class → `Class.x()`→that class → same-file → global. 1 match =
    INFERRED, many = AMBIGUOUS, 0 = external/miss. Computes **Coverage** (resolved/
    ambiguous/unresolved/external; `pct` excludes external).
  - *SCIP* (`scip_resolver.py`): runs scip-python(Pyright) → `index.scip` → maps symbols to
    our nodes **by definition location** → emits **EXTRACTED** CALLS. Pipeline then drops
    heuristic *Python* CALLS and substitutes these; everything else stays heuristic.
- **3 Write (`store.py`)** — bootstrap unique-id constraint, repo-scoped wipe, batched
  MERGE upserts (1000/batch) for nodes and edges.

### Graph schema (what's actually in Neo4j)
Every node also carries the shared label **`:CodeNode`** (owns the unique `id` index).

**Node labels** (allowlisted in `schema.py`): `File`, `Class`, `Function`, `Field`,
`Annotation`. (`Module` is allowlisted but not yet emitted.)

**Node properties** (set when present; empties dropped by `_clean`):
`id` · `repo` · `name` · `fqn` · `kind` · `lang` · `file` ·
`start_line`/`start_col`/`end_line`/`end_col` ·
`display_name` · `visibility` · `modifiers` (list) ·
`is_static`/`is_abstract`/`is_async` · `return_type` · `param_count` ·
`signature` · `docstring` · `body_hash` · `extractor` · `confidence` · `last_indexed`

**Stable ID:** `id = sha1(repo + kind + fqn)[:16]` — *not* line-based, so editing a body
changes `body_hash` but keeps `id` (future incremental re-index patches in place).
`repo` is on every node → **multiple repos coexist** in one DB (e.g. `demo-java` +
`primitive-pr`); `wipe` is repo-scoped.

**Edge types** (allowlisted): `CONTAINS`, `IMPORTS`, `CALLS`, `INSTANTIATES`, `EXTENDS`,
`IMPLEMENTS`, `ANNOTATED_WITH`.
```
File  ─CONTAINS→  Class / Function / Field        Function ─CALLS→ Function (EXTRACTED via SCIP, Py)
Class ─CONTAINS→  Function / Field / nested Class  Function ─INSTANTIATES→ Class
Class ─EXTENDS→   Class                            File ─IMPORTS→ Class (in-repo only)
Class ─IMPLEMENTS→ Class (Java)                    Class/Function ─ANNOTATED_WITH→ Annotation
```
**Edge properties:** `confidence` (EXTRACTED|INFERRED|AMBIGUOUS) · `origin`
(EXTRACTED|DERIVED — orthogonal to confidence) · `extractor` (tree-sitter|scip-python|
heuristic) · `evidence_file`/`evidence_line`/`evidence_col` (where the evidence lives).

### Two quality axes
- **confidence** = how sure the *resolution* is (EXTRACTED precise / INFERRED guess /
  AMBIGUOUS multi). Python CALLS are EXTRACTED (SCIP); Java CALLS + non-CALLS are INFERRED.
- **origin** = how the fact entered (EXTRACTED from AST/index vs DERIVED by later analysis).
  Everything today is EXTRACTED-origin; Milestone-5 analyses will be DERIVED.

### SCIP subsystem (precise Python resolver)
```
scip-python (Pyright)  →  index.scip  →  scip_resolver:
  cwd = repo root          per-occurrence   1. parse protobuf
  emits global symbol      symbol + roles   2. symbol→node by DEFINITION location
  per occurrence + roles   (def/read/import) 3. non-def fn occurrence = call →
  (no WriteAccess!)                             enclosing fn by line range
                                             4. emit EXTRACTED CALLS (+ evidence)
```
Binary under `scip_tooling/node_modules` (gitignored), located via `config.scip_python_bin()`
(env → packaged → PATH). Graceful fallback: if unavailable/fails, `available=False` and the
heuristic CALLS are kept.

### Storage / runtime
Neo4j 5 + GDS in Docker (`cbrain-neo4j`, bolt :7687). Python 3.14, tree-sitter 0.25,
neo4j driver 6.2, protobuf, scip-python 0.6.6, JDK 25. Measured: `primitive-pr` = 24 files
→ 430 nodes / 841 edges, ~3s incl. SCIP indexing; Python CALLS 100% EXTRACTED.

### What is NOT built yet (the honest boundary)
- **Java CALLS** still heuristic (scip-java needs maven/gradle — not installed).
- **No richer edges yet:** no `HAS_TYPE`/`RETURNS`/`OVERRIDES`/`READS`/`WRITES` (Milestones
  2–4) — though SCIP already carries most of the data; we only consume CALLS.
- **No instance-field modeling** (only class-level fields).
- **No static analysis** (CFG/DFG/PDG — Milestone 5) and **no canonical IR** (Milestone 6).
- **No semantic layer:** no summaries, embeddings, vector/hybrid search, context packs,
  generation, incremental re-index, or export.

---

## ✅ DONE — Phase 0 (graph building) works end-to-end

Pipeline: `discover → tree-sitter extract (Java + Python) → heuristic name-resolution → Neo4j`.

- **Package `graph_rag/`** built and verified:
  - `discovery.py` — walk repo, lang-detect, hash (Stage 0)
  - `languages.py` — tree-sitter parser loading (ts 0.25 API)
  - `extractors/java.py`, `extractors/python.py`, `extractors/common.py` — Stage 1
  - `resolver.py` — heuristic name resolution + **coverage metric** (Stage 2, partial)
  - `store.py` — Neo4j bootstrap (unique `:CodeNode(id)`) + batched upserts
  - `pipeline.py`, `cli.py` — orchestration + `index` command
  - `ids.py`, `models.py`, `schema.py`, `config.py` — stable IDs, data model, allowlists, env config
- **Nodes:** File, Class (class/interface/enum/record), Function (method/constructor/function), Field, Annotation. Stable `id = hash(repo+kind+fqn)` + shared `:CodeNode` label.
- **Edges:** CONTAINS, IMPORTS, CALLS, INSTANTIATES, EXTENDS, IMPLEMENTS, ANNOTATED_WITH — each tagged `confidence` (EXTRACTED|INFERRED|AMBIGUOUS).
- **Verified:** `demo-java` sample + `primitive-pr` (13 py files → 352 nodes / 741 rels in ~0.7s). Live Cypher confirmed containment, class hierarchy, and blast-radius (transitive callers).

### Environment (already set up)
- venv at `graph_rag/.venv` with tree-sitter 0.25, tree-sitter-java/python, neo4j 6.2,
  **protobuf** (reads the SCIP index).
- **SCIP (Python resolver):** `scip-python 0.6.6` installed under
  `graph_rag/scip_tooling/node_modules` (gitignored). Reinstall with
  `npm install --prefix scip_tooling @sourcegraph/scip-python@0.6.6`. Proto bindings
  vendored at `graph_rag/scip/scip_pb2.py` (regen: `python -m grpc_tools.protoc
  -Igraph_rag/scip --python_out=graph_rag/scip graph_rag/scip/scip.proto`).
- Neo4j 5 + GDS in Docker: container `cbrain-neo4j`, bolt `:7687`, browser `:7474`, auth `neo4j/testpassword`.
- Run: `NEO4J_PASSWORD=testpassword ./.venv/bin/python -m graph_rag.cli index <path> --repo NAME`
  (add `--no-scip` to use only the heuristic resolver).

### Current health signal (the important honest number)
- **Python CALLS are now EXTRACTED via SCIP (Pyright)** — type-precise, cross-file.
  On `primitive-pr`: **415 EXTRACTED CALLS edges**, 811/1284 in-repo symbols mapped,
  ~2.4s including indexing. The heuristic these replace was **84.8% precise / 92.5%
  recall** vs SCIP (69 wrong edges, 31 missed) — both classes of error gone.
- **Heuristic remains the fallback** (Java, or `--no-scip`): scope-aware Python resolution
  still scores **98.2%** of in-repo call-sites with only ~11–25 ambiguous (vs old global
  bare-name 89.1% / 66 ambiguous). The earlier ~21% headline was an artifact of counting
  stdlib/builtin calls as failures; the metric now splits **external** from genuine misses.
- Java path verified unaffected (SCIP skipped when no Python; heuristic CALLS retained).
- Probe (no Neo4j, heuristic only): `./.venv/bin/python measure_coverage.py <path> --repo NAME`.

---

## 🛠 CHANGELOG — Stage 2 Python scope-aware resolution (latest work)

> Read this to know exactly what changed in code and why. Plain-English first,
> then the per-file diff and the data flow.

### The problem it fixes
Stage 1 (the Python extractor) used to throw away the **receiver** of every call.
`self.foo()`, `mod.foo()`, and a plain `foo()` were all flattened to the bare name
`"foo"` by `_dotted_tail(...)`. So Stage 2 (the resolver) had nothing to match on
except the bare name, against a **global** index of every function in the repo.
Result: lots of wrong/ambiguous matches, and a "21%" health number that was really
just an artifact of counting `os.path.join` / `len()` / etc. as failures.

### What changed, in plain words
1. We now **remember who the call was made on** (the receiver).
2. The resolver uses that receiver + lexical scope to pick the *right* target instead
   of guessing from a global list.
3. The health metric now **excludes calls that can't possibly be in-repo** (stdlib /
   3rd-party / builtins), so the percentage reflects real resolution quality.

### Files changed (what each one now does)

- **`graph_rag/models.py`** — `RawRef` gained one field: `recv: str`.
  A `RawRef` is an "edge whose target is only a name, resolved later." `recv` records
  the call receiver: `"self"`/`"cls"`, a module/class/variable name, or `""` for a
  bare `foo()`. Old refs implicitly had `recv=""`, so Java (which doesn't set it) is
  unchanged.

- **`graph_rag/extractors/python.py`** (Stage 1) — two changes:
  - `_receiver(src, fn)` — new helper. If the call's function node is an `attribute`
    (`X.foo`), it returns the tail name of `X` (`self`, the module alias, etc.);
    for a bare identifier call it returns `""`. The result is stored on the `CALLS`
    `RawRef` via `recv=`.
  - `_calls_in_scope(block)` — new helper that replaces the old
    `iter_descendants(body)` call-collection. It walks the function body but **stops
    at nested `def`/`class`** boundaries. This fixes a double-count bug: previously a
    call inside a nested function was attributed to **both** the inner and the outer
    function (because the outer scan saw all descendants, then the nested def was
    walked again). Now each call belongs to exactly one enclosing function.

- **`graph_rag/resolver.py`** (Stage 2) — the core of the change:
  - `resolve(...)` now also takes the **structural `edges`** (the `CONTAINS` edges
    from Stage 1), because resolution needs the containment tree to know "what class
    is this call inside" and "what methods does that class have."
  - It builds three lookup tables: `parent_of` (child→parent from CONTAINS),
    `methods_of_class` (class id → {method name → method nodes}), and
    `classes_by_name`. Plus `enclosing_class_id(node_id)` walks `parent_of` up to the
    nearest Class.
  - New `narrow_call(ref)` is the resolution cascade for a `CALLS` ref, in priority
    order:
    - **(a)** `recv` is `self`/`cls` → look up the name in the **enclosing class's**
      methods.
    - **(b)** `recv` is the name of an in-repo class → look up the name in **that
      class's** methods (covers `Foo.bar()` static/factory calls).
    - **(c)** otherwise → prefer a definition in the **same file** before falling back
      to the global name index.
    The first rule that yields candidates wins. 1 candidate → resolved (`INFERRED`
    edge); >1 → `AMBIGUOUS` edges to all; 0 → not an in-repo target.
  - `Coverage` gained `external` plus an `inrepo` property. `emit(...)` now takes
    `known_in_repo` (`ref.target_name in by_name`): a 0-candidate ref is counted as
    `external` when the name doesn't exist anywhere in-repo, vs `unresolved` when it
    does (a genuine miss). `pct()` changed from `resolved/total` to
    `resolved/inrepo` — the honest "of the calls that *could* resolve, how many did."

- **`graph_rag/pipeline.py`** — passes `all_edges` into `resolve(...)` (one-line
  signature change to feed it the CONTAINS edges).

- **`graph_rag/cli.py`** — the `index` command's coverage printout now shows the
  `external` column and labels `%` as "of in-repo targets."

- **`graph_rag/measure_coverage.py`** (new dev script, no Neo4j) — runs
  discover→extract→resolve in memory and prints: per-edge coverage, a CALLS
  receiver-shape census (`self/cls` vs `recv.name()` vs `bare()`), and an
  old-vs-new lift (it re-runs the *old* global bare-name logic on the same inputs
  via `_baseline_calls` so the comparison is apples-to-apples).

### Data flow after the change (unchanged stages in grey)
```
discover ─► extract (Stage 1) ─────────────────► resolve (Stage 2) ─► store
            • Nodes + CONTAINS edges                • narrow_call(): self→class,
            • CALLS RawRef now carries recv           Class.m→class, else same-file→global
            • calls counted once per scope          • Coverage splits external vs in-repo
```

### How to see it / regenerate the numbers
`./.venv/bin/python measure_coverage.py ../primitive-pr --repo primitive-pr`

### Known gap left on purpose
`module.func()` is **not** bound through imports yet — `recv` is captured but we don't
map import aliases → module fqns, so such a call falls to the same-file/global path
(usually it's external anyway). See the TODO note below.

---

## 🛠 CHANGELOG — SCIP (Pyright) resolver for Python CALLS (latest work)

> This replaces the name-heuristic for **Python CALLS** with type-precise resolution.
> Heuristic stays as the fallback (Java, or when SCIP can't run).

### Why
Measured against SCIP as ground truth on `primitive-pr`, the heuristic CALLS graph was
**84.8% precise / 92.5% recall**: 69 edges were *wrong* (name collisions resolved to the
wrong function) and 31 real cross-file/typed calls were *missed*. Wrong edges are poison
for PR-review blast-radius. SCIP (Pyright) resolves every occurrence to its true global
symbol, so those go away — at `EXTRACTED` confidence.

### How it works (data flow)
```
scip-python (Pyright)  ──►  index.scip (protobuf)  ──►  scip_resolver  ──►  EXTRACTED CALLS
   runs over the repo        per-occurrence symbols      map to our nodes     (pipeline swaps
   (cwd = repo root)         + roles (def/read/import)    by def location       out heuristic
                                                          + line-range          python CALLS)
```
1. `scip-python index` emits, for every identifier occurrence, the **global symbol** it
   refers to and role bits (Definition / Read / Import / Write).
2. **Symbol→node mapping by definition location** (not by parsing SCIP descriptor sigils):
   a symbol's *definition* occurrence gives `(file, line)`; we already store
   `(file, start_line)` on every node → match. (811/1284 in-repo symbols map; the rest are
   module globals / params / locals we deliberately don't model as nodes.)
3. A **non-definition** occurrence of a *function* symbol is a call. Its line, placed
   against our Function line-ranges, gives the enclosing call site →
   `CALLS(site → target)` at EXTRACTED.

### Files
- **`graph_rag/scip/`** — vendored `scip.proto` + generated `scip_pb2.py` (+ `__init__`).
- **`graph_rag/scip_resolver.py`** — `run_scip_python()` (invoke the indexer, cwd=repo),
  `resolve_calls()` (parse + map + emit), `scip_resolve()` (high-level: index→resolve,
  returns `[]` + `available=False` on any failure so the pipeline falls back). `ScipReport`
  carries tool/version, docs, symbols mapped, edge count.
- **`graph_rag/config.py`** — `scip_python_bin()` locates the indexer
  (`$SCIP_PYTHON_BIN` → packaged `scip_tooling` → PATH).
- **`graph_rag/pipeline.py`** — after the heuristic pass, if the repo has Python and SCIP
  is available: **drop heuristic Python CALLS, add SCIP CALLS**, and remove `CALLS` from
  the heuristic coverage report (it's superseded). Non-Python CALLS (Java) are kept.
  `IndexResult.scip` holds the report.
- **`graph_rag/cli.py`** — prints the SCIP line; `--no-scip` forces heuristic-only.
- **`requirements.txt`** — added `protobuf`; documented the npm/grpc-tools setup.

### Important findings (locked-in facts)
- **Run cwd matters:** scip-python discovers files relative to the working directory; we
  run it with `cwd=repo_root` (passing `--cwd` with an absolute `--output` indexed 0 files).
- **READS are free** from SCIP roles (2113 occ), **but this scip-python build never emits
  WriteAccess** (0 WRITE occ). So state-impact WRITES still need AST later — don't expect
  them from SCIP. We currently consume neither (instance fields aren't modeled as nodes
  yet), to avoid shipping a half-populated edge that lies.
- **scip-java is blocked** (needs maven/gradle; none installed). Java CALLS stay heuristic.

### Verify
```
./.venv/bin/python -m graph_rag.cli index ../primitive-pr --repo primitive-pr   # full (Neo4j)
# offline edge counts: scip_resolve() in measure_coverage-style harness (see git history)
```

---

## 🛠 CHANGELOG — Milestone 1: Metadata & Provenance (latest work)

> Makes every node richly described and every edge **traceable to its evidence**.
> Provenance is the foundation everything else (and any future agent) leans on.

### What changed
- **`models.py`** — added `Origin` enum (`EXTRACTED` vs `DERIVED`, *orthogonal* to
  `Confidence`). `Node` gained columns (`start_col`/`end_col`), `display_name`,
  `visibility`, `modifiers` (list), `is_static`/`is_abstract`/`is_async`, `return_type`,
  `param_count`, `docstring`, `extractor`. `Edge` gained `origin`, `extractor`,
  `evidence_file`/`evidence_line`/`evidence_col` and a `props()` method. `RawRef` gained
  `ref_file`/`ref_line`/`ref_col` (the reference-site location, so resolved edges know
  their evidence). `_clean()` drops empty values (incl. `False`/`0`/empty list).
- **`extractors/python.py`** — populates all the above. `visibility` from the underscore
  convention; `is_static`/`is_abstract` from `@staticmethod`/`@classmethod`/
  `@abstractmethod`; `is_async` from the `async` child token; `return_type` from `-> T`;
  `param_count` excludes a leading `self`/`cls`; `docstring` = first string in the body.
  New `contains()`/`ref()` helpers stamp provenance + location on every edge/ref.
- **`extractors/java.py`** — same metadata from Java `modifiers` (visibility keyword,
  static/final/abstract, annotations), method return type, formal-parameter count; same
  provenance helpers.
- **`resolver.py`** — `make_edge()` copies `RawRef` location → edge evidence; heuristic
  edges are `origin=EXTRACTED, extractor="heuristic"` (the reference is real; only the
  *resolution* is a guess, carried by `confidence`).
- **`scip_resolver.py`** — SCIP CALLS carry `extractor="scip-python"` and the exact
  occurrence `file:line:col` as evidence (one representative per distinct edge).
- **`store.py`** — `write_edges` now writes `r += row.props` (all provenance); both writers
  stamp `n.last_indexed` / persist edge props.

### Why provenance matters
An edge that can't say *"SCIP saw this at `graph.py:135:8`"* can't support a grounded
claim later. With `origin` + evidence, a future agent (or a human reviewer) can point at
the exact source for any relationship, and DERIVED analyses (Milestone 5) stay cleanly
separable from raw EXTRACTED facts and can be recomputed without reparsing.

### Verify
`NEO4J_PASSWORD=… ./.venv/bin/python -m graph_rag.cli index ../primitive-pr --repo primitive-pr`
then in Neo4j: `MATCH ()-[r:CALLS]->() RETURN r.extractor, r.origin, count(*)`.

---

## 🔜 ROADMAP (LOCKED — stop redesigning, execute)

> **Final direction:** finish a rock-solid **structural knowledge graph** (Phase 1), then
> build the **semantic → retrieval → context → agent** stack on top. Goal = best AI code-
> intelligence platform, *not* a compiler. **CFG/DFG/PDG are OFF the critical path** →
> Phase 6 (advanced analysis, only if/when the agent needs deeper reasoning). Parameters
> stay edges (not nodes). The schema is frozen except for the edge/metric types each
> milestone below adds — no more inventing edge types.

### Phase 1 — Complete the structural graph (the next 1–2 months)

**Milestone 1 — Metadata & Provenance ✅ DONE**
- [x] Rich node metadata (range+columns, visibility, modifiers, is_static/abstract/async,
      return_type, param_count, docstring, extractor, last_indexed) — Python + Java.
- [x] Edge provenance: `origin` (EXTRACTED vs DERIVED), `extractor`, evidence `file:line:col`.

**Milestone 2 — Type System ✅ DONE (tree-sitter cut)**
- [x] `RETURNS` (Function→Class), `OF_TYPE` (Field→Class), `HAS_TYPE` (param types),
      `HAS_GENERIC` (generic args, e.g. `List[User]`→User). From tree-sitter types (Java)
      and annotations (Python), name-resolved to in-repo Class (INFERRED); external types
      (str/List/Optional/…) correctly excluded. Parameters stay edges, not nodes.
      Verified: "where is type `Finding` used?" returns users + evidence loc. (+100 edges.)
- [ ] *Upgrade (in M3):* SCIP-precise Python types → these become EXTRACTED instead of
      INFERRED; add `BOUNDS` for class type-params. (Edge types live in `schema.py`.)

**Milestone 3 — Symbol Resolution (complete it)** ⭐⭐⭐⭐⭐
- [x] Python via SCIP: defs/refs/scopes/imports → EXTRACTED CALLS (84.8%P/92.5%R vs heuristic).
- [ ] **scip-java** for Java (blocked: needs maven/gradle). Surface aliases/imports/exports
      as first-class. Goal: every identifier resolves to exactly one symbol.

**Milestone 4 — Program Relationships** ⭐⭐⭐⭐⭐ (biggest gap today)
- [ ] `READS`, `WRITES`, `OVERRIDES`, `DECLARES`, `USES_TYPE`, `THROWS`, `CATCHES`.
      `OVERRIDES`/`READS` from SCIP; ⚠ `WRITES` needs AST (scip-python emits no WriteAccess).
      End state: the graph understands **state**.

**Milestone 5 — Static Metrics** ⭐⭐⭐⭐ (pure compiler facts, no LLM)
- [ ] Per Function: cyclomatic complexity, LOC, fan-in, fan-out, call/branch/loop count,
      recursion flag, pure/impure. All computed from the AST + the call graph.

**Milestone 6 — Canonical Code Model** ⭐⭐⭐⭐⭐
- [ ] Promote `models.Node/Edge` into an explicit language-neutral IR with per-language
      adapters (Java/Python → Canonical IR → Neo4j; Go later).

**Milestone 7 — Graph Validation Suite** ⭐⭐⭐⭐ (do early; guards everything)
- [ ] Every index auto-verifies invariants: every CALLS/OF_TYPE/RETURNS target exists,
      no dangling nodes / orphan edges, IDs stable & unique, duplicate detection, resolver
      accuracy, graph statistics. Fail loud on violation.

### ⏸ STOP — freeze the structural layer here. Then:

- **Phase 2 — Semantic:** identity (repo→module→class→function) → implementation flow → embeddings.
- **Phase 3 — Hybrid Retrieval:** graph + vector + keyword + code, merged.
- **Phase 4 — Context Builder:** question → graph expansion → neighbor/semantic/source selection → prompt assembly.
- **Phase 5 — Agent:** plan → retrieve → reason → edit → validate(vs graph) → reflect.
- **Phase 6 — Advanced Analysis (only if needed):** CFG / DFG / PDG, security, dead code, architecture-violation detection. DERIVED layer, separable.

---

## Notes / decisions (locked)
- Neo4j (power) + exportable artifact (portability). Engine: tree-sitter + scip/Pyright.
  **CFG/DFG/PDG via Joern/CPG is Phase 6, off the critical path** (not a compiler project).
- **Provenance is first-class:** `origin` (EXTRACTED vs DERIVED) on every edge, separate
  from `confidence`. Evidence `file:line:col` on every edge so claims are traceable.
- **Parameters are edges, not nodes** (signature is a property; type linkage via `HAS_TYPE`).
  Still cut: git layers, cross-repo lineage, literals.
- "Node = pointer to source + metadata + wiring." Intra-function detail stays out until the
  Phase-6 CFG/DFG/PDG layer, which is kept separable as DERIVED.
- Java resolves cleanly → prove resolution on Java; Python is lossier → SCIP carries it,
  heuristic + AMBIGUOUS tags are the fallback.
- **Roadmap is frozen.** New edge types only as a milestone above defines them.

## Stale-state warning
- The Docker container `cbrain-neo4j` may be stopped after a reboot:
  `docker start cbrain-neo4j` (data persists in the container).
- Re-indexing a repo wipes its nodes first (use `--no-wipe` to append).
