# Codebase Brain — Architecture (the vision)

> **Everything we are trying to build.** For what exists *right now*, see
> [`STATUS.md`](STATUS.md) and §15 below for full detail. Structural graph (Phase 1), semantic
> enrichment (Phase 2), embeddings + hybrid retrieval (Phase 3) are **built and validated**
> end-to-end (see §15). Direction for what's next: a **codebase analysis layer** (vulnerability/
> quality/design review with auto-fixable findings) — design in progress, see §16 and
> [`ANALYSIS_LAYER.md`](ANALYSIS_LAYER.md). CFG/DFG/PDG remain **off the critical path** (Phase 6,
> only if the agent needs deeper reasoning).

A multi-layer knowledge graph over an organization's legacy Java/Python code, used to power an
**agentic code generator** that builds new code grounded strictly in the existing codebase, and
(newer goal) a full codebase health/vulnerability review with auto-fixable output.

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
- **Phase 2 — Semantic:** identity → summaries → embeddings. ✅ **Built + validated** (see §15).
- **Phase 3 — Hybrid retrieval:** graph + vector + keyword, merged. ✅ **Built + validated** (see §15).
- **Phase 4 — Context builder:** question → graph expansion → selection → prompt assembly.
  ✅ **Built** — folded into `retrieval.py`'s EXPAND + CONTEXT PACK stages rather than a separate
  module (see §15).
- **Phase 5 — Agent:** plan → retrieve → reason → edit → validate(vs graph) → reflect. ⬜ Not built.
- **Phase 6 — Advanced analysis (only if needed):** CFG/DFG/PDG, security, dead code,
  architecture-violation detection — a separable DERIVED layer.
- **Phase 7 — Codebase analysis layer (new, design phase):** full vulnerability/correctness/
  design/standards review per function, auto-fixable findings, blast-radius propagation,
  re-runnable/cached. ⬜ **Design complete, not yet implemented** — see §16 and
  [`ANALYSIS_LAYER.md`](ANALYSIS_LAYER.md).

**Useful brain after Phase 2–3 (done); generating brain after Phase 5; reviewing brain after
Phase 7.**

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

---

## 15. Current State — What's Actually Built & Validated (2026-07-02)

> This section is the detailed, as-built companion to the roadmap in §12. It reflects real runs
> against the `sail-packages` repo (`D:/Downloads/sail/packages`), not aspiration.

### 15.1 Structural graph (Phase 1)
- Nodes: `Repository`, `Package`, `File`, `Class`, `Function`, `Field`, `Annotation`, `Endpoint`.
  Every node carries the shared label `:CodeNode` (single unique-id constraint `code_node_id`)
  plus its specific label — `store.py`'s `write_nodes()` does `SET n:{label}, n += row.props`.
- Edges: `CONTAINS`, `IMPORTS`, `CALLS`, `INSTANTIATES`, `EXTENDS`, `IMPLEMENTS`,
  `ANNOTATED_WITH`, `RETURNS`, `OF_TYPE`, `HAS_TYPE`, `HAS_GENERIC`, `OVERRIDES`, `READS`,
  `WRITES`, `THROWS`, `CATCHES`, `EXPOSES`, `CALLS_API`, plus derived `BELONGS_TO`/`USES` and
  event/auth edges `EMITS_EVENT`/`CONSUMES_EVENT`/`REQUIRES_AUTH`/`ENFORCES_POLICY`.
- **Security note (verified in `store.py`):** labels/edge-types are validated against a schema
  allowlist (`assert_label`/`assert_edge`) before being interpolated into Cypher — Cypher can't
  parametrize labels/rel-types, so this allowlist is the injection-prevention mechanism. Vector
  index dimension is similarly int-cast before interpolation, not attacker-controlled.
- Validated on `sail-packages` (5 packages: `sail_core`, `sail_evalvation`, `sail_ingestion`,
  `sail_observation`, `sail_retrieval`): **114 files, 612 nodes, 2317 edges**, validation ok=True.

### 15.2 Semantic enrichment (Phase 2) — Identity + Implementation Flow
Implemented in `semantic.py` + `llm.py`. Two artifacts only (contract-first, not prompt-first —
see `SEMANTIC_LAYER.md`):

- **Identity** (`enrich_identities()`, `SEMANTIC_VERSION="v2"`) — generated bottom-up **Function →
  Class → Package → Repository**. `FunctionIdentity` schema: `purpose`, `responsibility`,
  `business_goal`, `domain_concepts`, `collaborators`, `preconditions`, `postconditions`,
  `side_effects`, `importance`, `confidence`, `keywords`, `tags`, `concepts`.
  - **Verified context shape (`_function_identity_prompt`):** a function's identity prompt is
    built from its own signature/docstring/(optional source) plus only the **names** of
    callees/callers/reads/writes/returns — never the callees'/callers' identity text or
    internals. This is why cache invalidation for this layer is naturally shallow (see §16.5).
  - Class identity aggregates its key methods (ranked by visibility + fan-in, capped at
    `_MAX_CLASS_METHODS`); Package aggregates members; Repository aggregates packages.
  - Cached by `body_hash` + `semantic_version` — unchanged nodes are never re-billed.
  - Validated on `sail-packages`: **163 functions, 94 classes, 34 packages, 1 repository**, all
    generated with `failed=0` using Bedrock Nova Pro.
- **Implementation Flow** (`generate_flows()`, `FLOW_VERSION="v1"`) — **function-only** (verified:
  `_Q_FLOW_TARGETS` matches `(f:Function {repo:$repo})` only — no class/package/path-level flow
  object exists today). Produces typed steps from `FLOW_STEP_TYPES`: `validation`,
  `database_read`, `database_write`, `business_logic`, `external_api`, `cache`, `filesystem`,
  `message_queue`, `event`, `authorization`, `authentication`, `response`, `error`,
  `computation`, `unknown`.

### 15.3 Embeddings (Phase 3, vector leg)
- Default provider: local, offline `sentence-transformers` (`all-MiniLM-L6-v2`, 384-dim) — no
  API key needed; embeds the **identity document**, not raw code (token-efficient, and stable
  across trivial code reformatting since it embeds meaning, not syntax).
- Native Neo4j HNSW vector index `code_embedding`, cosine similarity
  (`store.create_vector_index()`), dimension interpolated safely (see security note above).
- Validated on `sail-packages`: **292 nodes embedded, dim=384, index ONLINE**.

### 15.4 Hybrid retrieval (Phase 3/4 combined — `retrieval.py`)
Already fully implements what §7–8 describe as aspiration. Verified pipeline, straight from the
module docstring:

```
question
  ├─ embed query (local, no key) ─► vector search (code_embedding)   ┐
  └─ keyword match (keywords/tags/concepts/name/fqn)                 ┘─► fuse (RRF, k=60)
                                                                           │
                          PRUNE #1 (LLM): drop candidates out of scope    │  ← skipped if no LLM
                          EXPAND: pull neighbors (callers/callees,        │
                            parent, reads/writes) + their identities      │
                          PRUNE #2 (LLM): drop irrelevant neighbors       │  ← skipped if no LLM
                          CONTEXT PACK: targets → full source;            │
                            neighbors → signature + identity (token win)  │
                          ANSWER (LLM): grounded answer + citations       │  ← skipped if no LLM
```

- Every stage is recorded on `AskResult.stages` (a `Stage` dataclass) — fully inspectable, not a
  black box.
- **Graceful degradation:** with no LLM configured, both prune passes and the final answer are
  skipped; retrieval still returns the ranked/expanded structure (`--no-llm` mode).
- Prune/dedup is **LLM-driven, never Python set-logic** (a standing design rule, noted in the
  code as `memory:`).
- Validated end-to-end on `sail-packages`: query *"how does llm answer"* correctly surfaced
  `sail_core.llms.base.LLMBase.generate_completion_response` as the top hit in `--no-llm` mode.

### 15.5 LLM provider
- `llm.py`: provider-agnostic, schema-validated structured extraction. `bedrock` provider routes
  `amazon.*` models (e.g. `amazon.nova-pro-v1:0`, the current default) through a **boto3-native
  Converse API path with `toolUse`** (`_boto3_extract`), since the `AnthropicBedrockMantle` path
  only supports Claude-family Bedrock models. `openai` and `anthropic.*` bedrock models use their
  own dedicated extract paths.

---

## 16. Planned — Codebase Analysis Layer (Phase 7, design phase)

> Full write-up: [`ANALYSIS_LAYER.md`](ANALYSIS_LAYER.md). This section summarizes the current
> design and every alternative considered, for a single point-in-time reference.

### 16.1 Goal
Not RAG — a full codebase review. Every function is analyzed for security vulnerabilities,
correctness bugs, reliability issues, design/architecture problems, optimization opportunities,
and coding-standards violations. **No default triage** — everything is analyzed and reported;
prioritization happens on the full output, not before. Every finding carries a blast-radius/
propagation chain. Output must be precise enough for an automated agent to auto-fix. Re-running
after a code change should only re-analyze what changed (cached, incremental).

### 16.2 Core architecture — three phases
| Phase | What runs | LLM? | Ordering needed? |
|---|---|---|---|
| **A — per-function analysis** | identity + flow of self, dependents, dependees **out to 2-3 hops** (not just 1 hop) — full source only if the model reports `needs_source` | Yes, once per function | No — fully parallel |
| **B — blast-radius propagation** | graph traversal over `CALLS` edges (visited-set; cycles are a non-issue) | No — pure Cypher | No |
| **C — severity reconciliation** | finding + blast-radius number as input, batchable | Yes, but cheap/batched | No |

**Why this shape:** Phase A only needs *identity* (already built by Phase 2, static/stable) for
neighbor context — never another node's in-progress findings. That removes the sequential-
ordering problem and the cycle problem for the expensive step. Widening context from 1 hop to
2-3 hops costs more tokens per call, **not** more calls (still one call per function) — but
fan-out compounds fast (≈5 neighbors at hop 1 can mean ≈125 at hop 3), so only hop-2/3 neighbors
relevant to a path (toward an `Endpoint` or a flagged sink) should be included, not all of them.

### 16.3 Graph-native taint analysis (complementary evidence source, not a replacement for Phase A)
- Deterministic Cypher reachability query: *"is there a path from an `Endpoint` parameter
  through `CALLS`/`RETURNS`/`WRITES` to a dangerous sink, without passing a sanitizer-tagged
  function?"* Reuses the same traversal machinery as Phase B — not a new phase/system.
- **Zero LLM calls in the traversal itself.** The only LLM touchpoint is a one-time cached tag
  per function (`acts_as_sanitizer: true/false`), computed once and reused by every future query
  — confirmed this does not cause recurring/repeated LLM calls.
- **Why it matters:** identity-based context (even at 2-3 hops) is a *summary* that can lose the
  "this receives untrusted input" signal across repeated hops (like a game of telephone) —
  exact graph traversal has no such decay. This specifically covers multi-hop injection-style
  vulnerabilities that neither 1-3 hop LLM context nor generic scanners reliably catch.
- **Semgrep/Bandit OSS considered and dropped:** both are pattern/AST matchers with no real
  interprocedural analysis (OSS editions) — high precision only for the exact literal pattern,
  low recall for the vulnerability class in general (easy to evade via refactoring/indirection),
  and prone to false positives absent dataflow awareness. The graph-native taint check covers
  the same ground more accurately using infrastructure already being built.

### 16.4 Duplicate findings across a call chain — real risk, mitigation planned
Since Phase A gives a node its dependees' full identity text, the LLM can "rediscover" a
dependee's issue and report it again on the caller (a root cause in D could surface as 4 separate
findings across an A→B→C→D chain, each with a different ID since the ID hash includes `fqn`).
- **Primary fix:** explicit prompt rule — only report a finding if the problematic code is
  physically inside *this* function's own body; dependency issues are linked via blast-radius
  propagation (Phase B), never re-reported.
- **Safety net:** embedding-similarity check between findings connected by a `CALLS` edge, reusing
  the existing embedding pipeline — no new infra needed.

### 16.5 Cache invalidation — confirmed bounded to hop 1
Verified against `_function_identity_prompt` (§15.2): identity only encodes **names** of
callees/callers, never their internals. So if callee C changes, only C's identity regenerates,
and only C's *direct* callers need finding re-analysis — it does not cascade further (a caller's
caller's own identity never changes as a result, since it's shallow/name-based). Caveat: this
bound holds only while identity generation stays name-based — re-verify if that ever changes.

### 16.6 Parallel execution & re-run efficiency
Per-node status field (`pending` → `in_progress` → `done`/`stale`, alongside `analysis_hash`).
Workers pull only `pending`/`stale` nodes — since Phase A needs no ordering, this can be a flat
queue, no dependency-graph scheduling required. Re-runs skip anything `done` whose hash hasn't
changed; crash-resume falls out of the same status field with no extra job-tracking system.

### 16.7 Alternatives considered and where they stand
- **Detection engine:** pure LLM (current) vs. hybrid deterministic-scanner-first — superseded by
  the graph-native taint idea for the injection-vulnerability class specifically; scanners (OSS)
  dropped (§16.3).
- **Unit of analysis:** function-level (current, kept — precise per-function auto-fix needs this)
  vs. class-level batching (rejected — blurs per-function fix instructions) vs. per-flow/path
  (folded in as the 2-3 hop context widening, not a separate phase, since flows are function-only).
- **Context flow between nodes:** parallel/identity-only (current, kept) vs. sequential/
  findings-propagated (rejected — reintroduces the ordering/cycle bottleneck the current design
  was built to avoid).
- **Model cost tiering** (cheap-model triage → escalate to strong model): good idea, **on hold**
  — needs a decision on exactly where to apply it before it's part of the plan, given the
  "no default triage" requirement.
- **Trigger model:** whole-repo batch analysis, confirmed as the requirement for this project
  specifically (PR-diff-scoped analysis is a separate, already-built system elsewhere).

### 16.8 Not yet designed
- Feedback loop (track auto-fixed vs. dismissed findings to calibrate future runs).
- Exact finding-ID scheme fix (needs a line-number/ordinal discriminator to avoid collisions when
  one function has two distinct findings of the same category+subcategory).
- LLM severity-consistency rubric for Phase C (to reduce run-to-run variance since severity is
  judgment-based, not rule-based).
