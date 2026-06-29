# Codebase Brain — Architecture (the vision)

> **Everything we are trying to build.** For what exists *right now*, see
> [`STATUS.md`](STATUS.md). Direction is **locked**: finish a rock-solid **structural graph**
> (Phase 1), then build the **semantic → retrieval → context → agent** stack on top.
> CFG/DFG/PDG are **off the critical path** (Phase 6, only if the agent needs deeper reasoning).

A multi-layer knowledge graph over an organization's legacy Java/Python code, used to power an
**agentic code generator** that builds new code grounded strictly in the existing codebase.

---

## 1. Goal & Thesis

Organizations run on legacy Java/Python; new work mostly **modifies and extends** that base.
We build a "brain" of the codebase once, then serve cheap, precise context for understanding
and for generation.

**Thesis — index once, query cheap.** Grep-style agents do *search-time understanding*: they
re-discover the codebase on every task. We do *pre-computed understanding*: spend tokens once at
index time, then serve a small, precise context pack per task (the same reason databases build
indexes instead of scanning every query).

This is **GraphRAG over code as an alternative to fine-tuning** for codebase knowledge. A capable
base model still reasons/generates; the graph replaces the knowledge injection fine-tuning would
do — and adds freshness, exact facts, traceability, and **verifiable grounding** (reject code
whose symbols don't exist).

---

## 2. The Two Jobs

- **Job 1 — Retrieve & understand.** "What in the legacy code is relevant to X, how does it
  work, what contracts/data does it touch."
- **Job 2 — Generate & build.** Produce new code grounded strictly in Job 1's findings — only
  patterns, APIs, and data shapes that actually exist in the graph.

One graph powers both. Job 2 = Job 1 + a generation step constrained by what Job 1 returns.

---

## 3. Core Principles

1. **Index once, query cheap.** Expensive work is amortized at index time.
2. **One graph, many layers.** Shared node identities; layers are edge-types/annotations on the
   same nodes, not separate stores.
3. **A node = pointer to source + selection metadata + cross-function wiring.** The body stays
   in the file; the node holds signature, summary, embedding, `body_hash`, and a fetch pointer.
4. **The "is it in the source?" test.** Put something in the graph only if it's (a) navigation
   metadata, (b) cross-function/cross-file structure, or (c) a cross-cutting fact expensive to
   grep. Intra-function detail you'd fetch anyway stays out (until the Phase-6 dataflow layer).
5. **Catalog everything, build little.** A defined-but-empty type is free; a populated-but-stale
   one lies. Build only what we can populate **and keep fresh**.
6. **Confidence + origin on every edge.** `confidence` = EXTRACTED|INFERRED|AMBIGUOUS (found vs
   guessed); `origin` = EXTRACTED|DERIVED (read from source vs computed by later analysis). Plus
   evidence `file:line:col` so any claim is traceable.
7. **Freshness is the tax.** Pre-computation buys token efficiency at the cost of staleness; an
   incremental re-index pipeline is non-negotiable.
8. **Don't use the LLM for what the parser gives precisely.** Signatures/types/annotations come
   from static analysis exactly and for free; the LLM is only for semantics.
9. **No duplicate edges.** Never materialize a relation derivable from another (e.g. no
   `DECLARES` aliasing `CONTAINS`, no `USES_TYPE` aliasing the type edges). New edge types only
   when a milestone defines one.

---

## 4. System Architecture

```
        ┌─────────────── INDEX-TIME (expensive, once) ───────────────┐
Repo →  [Ingest] → [Resolve] → [Layer extractors] → [Semantic enrich]
           ↓           ↓              ↓                     ↓
                   ──────────  Graph Writer → NEO4J  ──────────
                                      ↓
                     [Community + god-nodes]   [Conventions]
        └─────────────────────────────────────────────────────────────┘

        ┌────────────── QUERY-TIME (cheap, per task) ────────────────┐
Request → [Decompose] → [Retrieve: vector + Cypher] → [Context Pack]
                                                          ↓
            Job 1: answer  |  Job 2: [Generate] → [Validate vs graph] → [Repair]
        └─────────────────────────────────────────────────────────────┘

[Incremental updater] ← git hook        [Exporter] → graph.json / report.md / graph.html
```

---

## 5. Graph Schema (Neo4j)

**Modeling rule:** use a `kind` property for variants; a **separate label** only when a node has
distinct edges or is queried independently (avoids label explosion). **Every node carries** a
stable `id = hash(repo + kind + fqn)`, `repo`, `fqn`, `confidence`, and (eventually) `summary` +
`embedding`. **Parameters are function metadata** (`param_names`/`param_types` arrays + type
edges), **not** separate nodes.

### Nodes
| Status | Labels |
|---|---|
| **Built** | `Repository`, `Package`, `File`, `Class`, `Function`, `Field`, `Annotation`, `Endpoint` |
| **Next (deterministic)** | `Module` (from build files: pom.xml/package.json/Cargo.toml) |
| **Semantic layer** | `Entity`, `Table`/`Column`, `DTO`/`Schema`, `Event`/`Topic`, `Config`/`ConfigKey`, `Library`/`Dependency`, `Concept`, `Feature`, `Community` (+god-node), `Convention`/`Pattern`, `Why`/`Rationale`, `Test` |
| **Catalog only** | `ExternalService`, `Migration`, `FeatureFlag`, `Transaction`, `ScheduledTask`, `Tier/Layer`, `Doc/ADR`, `BusinessRule`, `State/StateMachine`, `Exception` |

### Edges
| Status | Edges |
|---|---|
| **Built — structure** | `CONTAINS`, `IMPORTS` |
| **Built — references** | `CALLS`, `INSTANTIATES`, `EXTENDS`, `IMPLEMENTS`, `ANNOTATED_WITH` |
| **Built — types** | `RETURNS`, `OF_TYPE`, `HAS_TYPE`, `HAS_GENERIC` |
| **Built — relationships** | `OVERRIDES`, `READS`, `WRITES`, `THROWS`, `CATCHES` |
| **Built — HTTP-API** | `EXPOSES`, `CALLS_API` |
| **Semantic layer** | `INJECTS`, `CONFIGURES`, `DEPENDS_ON`, `QUERIES`, `MAPS_TO`, `PUBLISHES`, `CONSUMES`, `READS_CONFIG`, `GUARDED_BY`, `ABOUT`, `IMPLEMENTS_FEATURE`, `EXPLAINS`, `EXEMPLIFIES`, `SIMILAR_TO`, `MEMBER_OF`, `COVERS`, `MOCKS` |
| **Catalog only** | `DISPATCHES_TO`, `FLOWS_TO`, `MUTATES`, `FOREIGN_KEY`, `HAS_COLUMN`, `VIOLATES`, `CLONE_OF`, `CO_CHANGES_WITH` |

### Explicitly cut (and why)
- **Git/social layers** (blame, co-change), **cross-repo lineage** — not generation signal (yet).
- **CFG/DFG/PDG dataflow** — deferred to Phase 6 as a separable DERIVED layer (Joern/CPG), only
  if the agent needs it.
- **Parameters-as-nodes, literals** — parameters live as Function metadata; literals stay out.
- **Duplicate/alias edges** — forbidden (principle 9).

---

## 6. Index Pipeline (target)

| Stage | Builds | How | Confidence | Status |
|---|---|---|---|---|
| **0 Discover** | `File` | walk, detect lang, hash | EXTRACTED | ✅ |
| **1 Parse** | `Class`/`Function`/`Field`/`Annotation` + `CONTAINS`/`IMPORTS` + metadata | tree-sitter | EXTRACTED | ✅ |
| **2 Resolve** ⭐ | `CALLS`/`INSTANTIATES`/`EXTENDS`/`IMPLEMENTS`/`OVERRIDES`/type edges | scip-python (Py), heuristic (Java) | resolved→EXTRACTED, heuristic→INFERRED, multi→AMBIGUOUS | ✅ (Java CALLS heuristic; scip-java blocked) |
| **2b Derive** | `OVERRIDES` (hierarchy), `Repository`/`Package` tree, static metrics | deterministic over the resolved graph | INFERRED/derived | ✅ |
| **3 Integration** | `Endpoint`/`Entity`/`Table`/`Event`/`Config`/`DTO` + edges | framework/annotation patterns | INFERRED | ⏳ (Endpoint+CALLS_API done) |
| **4 Why** | `Why` + `EXPLAINS` | docstrings + NOTE/HACK/TODO + ADRs | EXTRACTED | ⬜ |
| **5 Semantics** | `summary` + `embedding` per node | LLM, batched, cached by `body_hash` | — | ⬜ |
| **6 Domain/Feature** | `Concept`/`Feature` + `ABOUT`/`IMPLEMENTS_FEATURE` | LLM over summaries+names | INFERRED | ⬜ |
| **7 Architecture** | `Community` + `MEMBER_OF` + god-nodes + `Convention` | GDS Leiden on CALLS+IMPORTS; centrality; LLM | — | ⬜ |

Stages 0–2b are per-file/cheap and done; 5–7 are the expensive LLM passes, run once and cached.

---

## 7. Semantic Enrichment & RAG (Stage 5+)

**Split parser vs LLM** — *parser (free, exact):* signatures, params, types, annotations, edges.
*LLM (English only):* one-line identity, behavioral summary, side effects, intent, concepts.

**Embed the summary, not the code.** One embedding doc per node, e.g.:
```
[OrderService.placeOrder] charges a customer and persists an order.
Side effects: writes orders table, publishes OrderPlaced.
Concepts: checkout, payment, order.  Signature: placeOrder(Cart, Customer): Order
```

**RAG techniques:** hybrid search (vector + BM25 over names/signatures); embedding **on the
Neo4j node** then **expand along edges** (graph expansion *is* the retrieval engine);
hierarchical summaries (function→class→module→community); contextual prefixing; optional
hypothetical-question embeddings on high-value nodes.

**Cost control:** cache by `body_hash`; tier models (cheap for routine summaries, strong for
class/module/community synthesis); template trivial members; dedicated embedding model.

---

## 8. Job 1 — Retrieve & Understand
1. **Decompose** request → sub-questions (LLM).
2. **Route** each to a primitive: *semantic find* (vector on summaries) · *structural traverse*
   (a fixed, audited set of parameterized **Cypher templates** — callers, callees, blast-radius,
   who-writes-table, endpoint→handler) · *conventions* (community/convention summaries) · *data
   shape* (persistence/API/event subgraph).
3. **Merge + dedup** — **LLM-driven**, not Python set-logic.
4. **Assemble a token-budgeted context pack:** target locations · exact contracts · conventions
   · data shapes · blast radius. **Target** node → full source; **neighbors** → signature +
   summary from the graph (no body fetch) — the core token win.

## 9. Job 2 — Generate & Ground
1. Generate constrained to **only symbols/patterns in the pack**.
2. **Validate:** tree-sitter-parse the output → check each referenced symbol **exists in the
   graph with a matching signature**.
3. **Repair loop:** on unknown-symbol/signature-mismatch, feed the correct contract from the
   graph; regenerate (bounded retries).
4. Emit code **+ provenance** (which graph nodes grounded it).

This validate→repair loop is what grep-agents and fine-tuned models structurally can't do.

---

## 10. Incremental Update & Export
- **Git post-commit hook:** diff changed files → re-run Stages 1–2b on those only → re-summarize/
  embed only nodes whose `body_hash` changed → patch edges → mark touched communities dirty.
- **Exporter:** dump a repo's subgraph → `graph.json` + `GRAPH_REPORT.md` + `graph.html`. Neo4j
  for power, portable files for distribution; union-merge driver for `graph.json`.

## 11. Tech Stack
Python orchestration · tree-sitter (Java + Python) · scip-python/Pyright (Python resolution;
scip-java when a JVM build exists) · Neo4j (Cypher, GDS Leiden + centrality, native vector index)
· Joern/CPG for the optional Phase-6 dataflow · LLM for semantics + an embedding model.

---

## 12. Roadmap (locked)

### Phase 1 — Structural graph (in progress)
- M1 Metadata & provenance ✅ · M2 Type system ✅ · M3 Symbol resolution (Python SCIP; scip-java
  blocked) ✅ · M4 Program relationships ✅ · M5 Static metrics ✅ · OVERRIDES (Java+Python) ✅ ·
  HTTP-API layer ✅ · Package/Repository ✅ · Param metadata ✅ · M7 Validation (baseline) ✅.
- **Remaining:** `Module` nodes from build files · M6 promote `models.Node/Edge` into a full
  language-neutral canonical IR with per-language adapters (Go later) · grow M7 invariants.

### Then — freeze structural, build up
- **Phase 2 — Semantic:** identity → summaries → embeddings.
- **Phase 3 — Hybrid retrieval:** graph + vector + keyword, merged.
- **Phase 4 — Context builder:** question → graph expansion → selection → prompt assembly.
- **Phase 5 — Agent:** plan → retrieve → reason → edit → validate(vs graph) → reflect.
- **Phase 6 — Advanced analysis (only if needed):** CFG/DFG/PDG, security, dead code,
  architecture-violation detection — a separable DERIVED layer.

**Useful brain after Phase 2–3; generating brain after Phase 5.**

---

## 13. Health metric — Resolution Coverage
The system hinges on Stage 2. Per repo, emit **% of call-sites EXTRACTED vs INFERRED vs
AMBIGUOUS** (external excluded). It's the single best signal of when a language is too lossy to
trust for generation. (`measure_coverage.py` for the heuristic; SCIP report for the precise path.)

## 14. Borrowed from Graphify
Confidence tags · "Why" nodes · god-node detection (high-connectivity hubs) · git-hook
incremental rebuild + merge driver. **Where we differ:** a real Neo4j graph (deep traversal +
communities + vector), precise resolution via language servers, and generation with verifiable
grounding (Job 2).
