# Codebase Brain — Architecture

> **⚠ Direction update (supersedes parts of this doc):** the build follows a **structural
> graph first, then semantic/agent** roadmap. Phase 1 (locked): types → symbol resolution →
> program relationships → static metrics → canonical IR → validation suite. Then semantic →
> retrieval → context builder → agent. **CFG/DFG/PDG are OFF the critical path** → Phase 6
> (advanced analysis, only if needed) — i.e. the dataflow cut in §5 mostly *stands*; only
> `THROWS`/`CATCHES` and a deferred dataflow layer come back, and parameters stay edges.
> Authoritative roadmap + progress: [`graph_rag/STATUS.md`](graph_rag/STATUS.md). This file
> remains the design reference for schema, principles, and the semantic/RAG/generation layers.

A multi-layer knowledge graph over an organization's legacy Java/Python code, used to
power an **agentic code generator** that builds new code grounded strictly in the
existing codebase.

---

## 1. Goal & Thesis

Organizations run on legacy Java/Python. New work mostly **modifies and extends** that
base. We build a "brain" of the codebase once, then serve cheap, precise context for
understanding and for generation.

**Thesis — index once, query cheap.** Tools like Copilot/Codex do *search-time
understanding*: grep + read files one-by-one on **every** task, re-paying discovery cost
each time. We do *pre-computed understanding*: spend tokens once at index time, then
serve a small, precise context pack per task. (Same reason databases build indexes
instead of scanning tables every query.)

This is **GraphRAG over code as an alternative to fine-tuning** for codebase knowledge.
We still use a capable base model to reason/generate; the graph replaces the knowledge
injection that fine-tuning would otherwise do — and adds freshness, exact facts,
traceability, and **verifiable grounding** (reject code whose symbols don't exist).

---

## 2. The Two Jobs

- **Job 1 — Retrieve & understand (RAG/QA over the graph).** "What in the legacy code is
  relevant to X, how does it work, what contracts/data does it touch."
- **Job 2 — Generate & build.** Produce new code grounded strictly in Job 1's findings —
  only patterns, APIs, and data shapes that actually exist in the legacy graph.

One graph powers both. Job 2 = Job 1 + a generation step constrained by what Job 1 returns.

---

## 3. Core Principles

1. **Index once, query cheap.** Expensive work is amortized at index time.
2. **One graph, many layers.** Shared node identities; layers are edge-types and
   annotations on the same nodes, not separate stores.
3. **A node = a pointer to source + selection metadata + cross-function wiring.**
   The function body stays in the file. The node holds: signature, summary, embedding,
   `body_hash`, and a fetch pointer (`file` + line range).
4. **The "is it in the source?" test.** Put something in the graph only if it is
   (a) navigation/selection metadata, (b) cross-function/cross-file structure, or
   (c) a cross-cutting fact expensive to grep. If it lives inside a single function body
   you'll fetch anyway (local control flow, local dataflow, raised exceptions), **leave
   it out**.
5. **Catalog everything, build little.** A defined-but-empty node/edge type is free; a
   populated-but-stale one lies. Only build what we can populate **and keep fresh**.
6. **Confidence tags on every inferred node/edge:** `EXTRACTED | INFERRED | AMBIGUOUS`.
   You always know what was found vs guessed. (Stolen from Graphify.)
7. **Freshness is the tax.** Pre-computation buys token efficiency at the cost of
   staleness; an incremental re-index pipeline is non-negotiable infrastructure.
8. **Don't use the LLM for what the parser gives precisely.** Static analysis produces
   signatures/types/annotations exactly and for free; the LLM is only for semantics.

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

### Modeling rule
Use **`kind` properties** for variants; **separate labels** only when a node has distinct
edges or is queried independently. (Avoids label explosion.)

### Every node carries
`id` (stable), `repo`, `fqn`, `summary` (LLM), `embedding` (vector), `confidence`.

**Stable ID:** `id = hash(repo + fqn + kind)` — never line-based. Editing a body changes
`body_hash`/`summary` but keeps the `id`, so incremental re-index patches in place.

### Nodes — build in v1

| Label | kinds / notes |
|---|---|
| `Repo` | |
| `File` | path, lang, hash |
| `Module` / `Package` | |
| `Class` | `kind: class \| interface \| enum \| record \| trait` |
| `Function` | `kind: method \| constructor \| function \| lambda`; holds `signature` (string property) |
| `Field` / `Property` | |
| `Annotation` / `Decorator` | the annotation itself (`@Transactional`, custom decorators) |
| `Entity` | domain/ORM object |
| `Table`, `Column` | physical schema (distinct from Entity/Field) |
| `DTO` / `Schema` | request/response/serialized shapes |
| `Endpoint` | REST/gRPC/GraphQL |
| `Event` / `Topic` | |
| `Config` / `ConfigKey` | |
| `Library` / `Dependency` | third-party packages |
| `Concept` | business ontology term |
| `Feature` | capability |
| `Community` | cluster (+ `is_god_node` flag, summary) |
| `Convention` / `Pattern` | "how we do X here" — generator imitates these |
| `Why` / `Rationale` | NOTE/HACK/TODO/docstring/ADR |
| `Test` | + `Mock`/`Fixture` |

### Nodes — catalog only (build with their layer)
`ExternalService`, `Migration`, `FeatureFlag`, `Transaction`, `ScheduledTask`,
`Tier/Layer`, `Doc/ADR`, `BusinessRule`, `State/StateMachine`, `Exception`.

### Edges — build in v1 (each tagged with `confidence`)

| Family | Edges |
|---|---|
| Structure | `CONTAINS`, `IMPORTS` |
| References | `CALLS`, `INSTANTIATES`, `EXTENDS`, `IMPLEMENTS`, `OVERRIDES`, `HAS_TYPE`, `RETURNS` |
| Wiring | `INJECTS`, `ANNOTATED_WITH` / `DECORATED_BY`, `CONFIGURES`, `DEPENDS_ON` |
| Data | `READS`, `WRITES`, `QUERIES`, `MAPS_TO` |
| Integration | `EXPOSES`, `PUBLISHES`, `CONSUMES`, `READS_CONFIG`, `GUARDED_BY` |
| Meaning | `ABOUT`, `IMPLEMENTS_FEATURE`, `EXPLAINS`, `EXEMPLIFIES`, `SIMILAR_TO` |
| Architecture | `MEMBER_OF` |
| Test | `COVERS`, `MOCKS` |

### Edges — catalog only
`DISPATCHES_TO`, `THROWS`/`CATCHES`, `FLOWS_TO`, `MUTATES`, `CALLS_ENDPOINT`,
`TIER`, `VIOLATES`, `CLONE_OF`, `CO_CHANGES_WITH`, `FOREIGN_KEY`, `HAS_COLUMN`.

### Explicitly CUT (and why)
- **Git layers** (ownership/blame, co-change) — social/risk signal, not generation. Co-change parked for later if blast-radius proves incomplete.
- **Cross-repo lineage** (`DERIVED_FROM`) — single-repo brain for now.
- **Exception nodes / dataflow / parameters-as-nodes / literals** — mostly still cut.
  Revisions: `THROWS`/`CATCHES` come in (Milestone 4); CFG/DFG/PDG dataflow is **deferred to
  Phase 6** (advanced analysis, only if needed), as a separable DERIVED layer via Joern/CPG.
  **Parameters remain edges, not nodes** (signature is a property; type linkage via
  `HAS_TYPE`). Literals still cut.

---

## 6. Index Pipeline

| Stage | Builds | How | Confidence |
|---|---|---|---|
| **0 Discover** | `File` | walk repo, detect lang, hash | EXTRACTED |
| **1 Parse** | `Class`/`Function`/`Field` + `CONTAINS`/`IMPORTS` + `Annotation` | **tree-sitter** | EXTRACTED |
| **2 Resolve** ⭐ | `CALLS`/`INSTANTIATES`/`EXTENDS`/`IMPLEMENTS`/`INJECTS`/types | **scip-java** (Java), **Pyright/scip-python** (Python) | resolved→EXTRACTED, heuristic→INFERRED, unresolved→AMBIGUOUS |
| **3 Integration** | `Entity`/`Table`/`Endpoint`/`Event`/`Config`/`DTO` + edges | framework/annotation patterns (`@Entity`, `@RestController`, Flask/FastAPI, Kafka, `@Value`/`os.environ`); `GUARDED_BY` from auth annotations | INFERRED |
| **4 Why** | `Why` + `EXPLAINS` | docstrings + NOTE/HACK/TODO + ADRs | EXTRACTED |
| **5 Semantics** | `summary` + `embedding` on each node | **LLM, batched, cached by `body_hash`** | — |
| **6 Domain/Feature** | `Concept`/`Feature` + `ABOUT`/`IMPLEMENTS_FEATURE` | LLM over summaries+names | INFERRED |
| **7 Architecture** | `Community` + `MEMBER_OF` + god-node flag + `Convention` | **Neo4j GDS Leiden** on CALLS+IMPORTS; centrality; LLM summarize communities; detect repeated structure → conventions | — |

Stages 1–4 are per-file and cheap; 5–7 are the expensive passes, run once and cached.

---

## 7. Semantic Enrichment & RAG Spec (Stage 5 detail)

**Split parser vs LLM:**
- *Parser (free, exact):* signature, params, types, returns, annotations, visibility, edges.
- *LLM (English only):* one-line **identity**, behavioral summary, **side effects**, intent, domain concepts.

**Embed the summary, not the code.** Build an embedding document per node:
```
[OrderService.placeOrder] charges a customer and persists an order.
Side effects: writes orders table, publishes OrderPlaced.
Concepts: checkout, payment, order.  Signature: placeOrder(Cart, Customer): Order
```

**RAG techniques:**
1. **Hybrid search** — vector (semantic) + keyword/BM25 over names+signatures. Always both.
2. **Embedding on the Neo4j node** — vector-seed, then **expand along edges**. This graph
   expansion after vector seeding *is* the retrieval engine.
3. **Hierarchical summaries** — function → class → module → community; retrieve coarse, drill fine.
4. **Contextual prefixing** — prepend parent class/module/role before embedding.
5. **Hypothetical questions** (optional, high-value nodes only) — embed "questions this
   function answers" to match question-shaped queries.

**Index-time cost control:**
- Cache by `body_hash` (re-embed only changed nodes).
- Tier models: cheap tier (e.g. `gpt-4o-mini`) for routine summaries; strong model for
  class/module/community synthesis.
- Template trivial members (getters/setters/`__init__`) — no LLM call.
- Dedicated embedding model (e.g. `text-embedding-3`) for vectors.

---

## 8. Job 1 — Retrieve & Understand

1. **Decompose** request → sub-questions (LLM).
2. **Route** each sub-question to a retrieval primitive:
   - *semantic find* → vector index on summaries
   - *structural traverse* → parameterized **Cypher templates** (callers, callees,
     blast-radius, who-writes-table) — a fixed, audited set, the retrieval agent's "tools"
   - *conventions* → `Community`/`Convention` summary fetch
   - *data shape* → persistence/API/event subgraph
3. **Merge + dedup** — **LLM-driven dedup**, not Python set-logic.
4. **Assemble the context pack** (token-budgeted): target locations · exact contracts
   (signatures/types) · conventions (community summary + 1–2 examples) · data shapes ·
   blast radius.

Token pattern: **target** node → fetch full source; **neighbors** → signature + summary
from the graph (no body fetch). The graph supplies neighbor contracts without fetching a
single neighbor body — the core token win.

---

## 9. Job 2 — Generate & Ground

1. Feed the context pack to the generator, constrained to use **only symbols/patterns in the pack**.
2. **Validate:** tree-sitter-parse the output → extract referenced symbols → check each
   **exists in the graph with a matching signature**.
3. **Repair loop:** on `unknown symbol`/`signature mismatch`, feed the error + the correct
   contract from the graph; regenerate. Bounded retries.
4. Emit code **+ provenance** (which graph nodes it was grounded in).

This validate→repair loop is what grep-agents and fine-tuned models structurally can't do.

---

## 10. Incremental Update & Export

- **Git post-commit hook:** diff changed files → re-run Stages 1–4 on those files only →
  re-summarize/embed only nodes whose `body_hash` changed → patch edges → mark touched
  communities dirty for periodic recompute.
- **Exporter:** dump a repo's subgraph → `graph.json` + `GRAPH_REPORT.md` + `graph.html`.
  **Neo4j core for power, portable files for distribution.** Ship a merge driver for
  `graph.json` (union-merge, no conflict markers).

---

## 11. Tech Stack

- **Orchestration:** Python.
- **Parsing:** tree-sitter (Java + Python, uniform).
- **Resolution:** scip-java (Java); Pyright / scip-python (Python).
- **Dataflow (later, on-demand):** Joern / Code Property Graph.
- **Store:** Neo4j — Cypher, GDS (Leiden + centrality), native vector index.
- **LLM:** current stack OpenAI `gpt-4o`; tier with a cheap model for routine summaries.
- **Embeddings:** dedicated embedding model (`text-embedding-3`).

---

## 12. Build Phases

| Phase | Stages | Proves |
|---|---|---|
| **0** | 0–2 + Cypher templates | "what calls X / 3-hop blast radius" — **Java first** (clean resolution), then Python |
| **1** | 5 + vector + context pack | "where do I implement feature X" (Job 1 QA) |
| **2** | 7 + 4 | architecture map, god-nodes, conventions, why-nodes |
| **3** | 3 + Job 2 generate/validate | grounded code generation |
| **4** | export + git hook | portable artifact, freshness |

Useful brain after **Phase 1**; generating brain after **Phase 3**.

### Phase 0 task breakdown
1. Repo walker + language detection + file hashing → `File` nodes.
2. tree-sitter integration (Java) → `Class`/`Function`/`Field`/`Annotation` + `CONTAINS`/`IMPORTS`.
3. Neo4j schema bootstrap: constraints on `id`, indexes, stable-ID hashing.
4. scip-java resolution → `CALLS`/`INSTANTIATES`/`EXTENDS`/`IMPLEMENTS`/`INJECTS` with confidence tags.
5. Parameterized Cypher templates: callers, callees, N-hop blast radius.
6. **Resolution coverage metric** (see §13).
7. Repeat 2 + 4 for Python (tree-sitter + Pyright).

---

## 13. Health Metric — Resolution Coverage

The whole system hinges on Stage 2. Emit per repo:
**% of call-sites resolved `EXTRACTED` vs `INFERRED` vs `AMBIGUOUS`.**
This is the single best health signal — it tells you when a language (esp. Python) is too
lossy to trust for generation.

---

## 14. What We Borrowed From Graphify

- Confidence tags (`EXTRACTED`/`INFERRED`/`AMBIGUOUS`).
- "Why" nodes (comments/docstrings/ADRs as first-class, linked to code).
- God-node detection (high-connectivity = blast-radius hubs).
- Git-hook incremental rebuild + merge driver for the exported artifact.

**Where we differ:** real Neo4j graph (not flat files) for deep traversal + communities +
vector; precise resolution (language servers, not tree-sitter alone); generation +
verifiable grounding (Job 2); richer typed layers (persistence/API/events/conventions).
