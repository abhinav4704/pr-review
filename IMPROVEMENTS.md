# Ontology Improvements — Toward a Change-Impact Knowledge Brain

Status: **proposal / backlog** (not yet implemented).

The current graph is a **call/structure graph**: it captures who-calls-whom,
routes, imports, auth, and table reads/writes, with every node carrying a
`file` + `line_start`/`line_end`. That is enough for PR-review retrieval.

To evolve it into a **knowledge brain** — "change X and immediately know what else
changes, without reading the code" — plus **token-efficient agentic retrieval**,
two gaps must close:

1. **Data/contracts are not first-class.** A Pydantic model is a single
   `VALIDATES_WITH` edge; a table is a single `READS_TABLE`/`WRITES_TABLE` edge. So
   "I renamed a field/column" cannot propagate.
2. **Nodes carry locations, not semantics.** An agent must slice full source to
   learn anything, which is token-expensive.

Everything below is **statically derivable** from the `ast` already walked in
`cpg/backend.py` (the optional `summary` field aside) and maps cleanly onto Neo4j
labels/relationships.

---

## Goal 1 — Change-impact propagation ("change X → what else changes")

| # | Add | Node / Edge | Why it unlocks impact |
|---|---|---|---|
| 1 | **Field/Schema granularity** | `Field` nodes under `Class`/`Schema`; edges `HAS_FIELD`, `READS_FIELD`, `WRITES_FIELD` | Change one field's type → every function/route/frontend call touching *that field* ripples, not the whole model. **Biggest single upgrade.** |
| 2 | **Column granularity** | `Column` nodes (or a `column=` attribute on the table edge) + `READS_COLUMN` / `WRITES_COLUMN` | "Renamed DB column" → only the functions touching it, not the whole table. |
| 3 | **Data-flow / contract edges** | `Function` → `Schema` via `ACCEPTS` / `RETURNS`; extend `Route` → `Schema` → `Field` | Closes the **full-stack loop**: DB column → table → function → route → `CALLS_ENDPOINT` → frontend → component. A backend response change now reaches the UI node. |
| 4 | **Test edges** | `Test` nodes + `TESTS` edges to functions/routes | "Change F → these tests cover / may break it." Core to a knowledge brain. |
| 5 | **Precomputed impact** | Build-time pass storing per node: `dependents` (transitive reverse-reachable ids, bounded), `dependents_count`, `blast_severity` (does it touch auth / writes?) | The agent reads the *answer* directly — no traversal, no source. |

### Example impact query (after #1 + #3)

```cypher
MATCH (f:Field {name:$field})<-[:READS_FIELD|WRITES_FIELD|ACCEPTS|RETURNS*1..4]-(x)
RETURN DISTINCT x.kind, x.name, x.file
```

This replaces "read the code to find consumers" with a single graph query.

---

## Goal 2 — Token-efficient agentic retrieval (reason without slicing source)

Make nodes **self-describing** so an agent reasons from ~30 tokens of metadata
instead of ~300 tokens of code. All computed at build time.

| Node attribute | Payoff |
|---|---|
| **`signature`** (params + types + return type) | Agent knows the interface without the body |
| **`side_effects`** (`reads` / `writes` / `raises` / `auth` / `async` flags) | "Does this mutate data / need auth?" with zero source |
| **`summary`** (one line; template-derived, or an optional cached LLM pass) | Build context from summaries; drill into source only for the few that matter |
| **`body_hash` / `sig_hash`** | (a) incremental rebuilds; (b) **graph-diff across commits** → "these 3 nodes changed" as seeds instead of a raw diff |
| **`layer` / `domain` tags** (`api` / `service` / `repo` / `model` / `ui`; `auth` / `chat` / …) | Scope retrieval to a subsystem → smaller, cheaper context |
| **`confidence`** on edges (resolved vs heuristic — extends existing `bridge_resolved` / `resolved`) | Agent trusts high-confidence edges, ignores noise → fewer false ripples, fewer tokens |

### Why `body_hash` is high-leverage

`body_hash` + the existing `CALLS` / data edges enables precise change detection:
diff two graphs → changed nodes become the **seeds**, the impact edges give the
**ripple**. An agent receives a precise "what changed and what it affects" payload
with almost no raw code.

---

## Priority / sequencing

**Tier 1 (do first — turns a call graph into a knowledge brain):**

- `Field` / `Schema` nodes + `ACCEPTS` / `RETURNS` / `READS_FIELD` edges (#1, #3)
- Per-node `signature` + `side_effects`

Together these answer "who depends on what data, and what each node *does*" — the
core difference between a call graph and a knowledge brain — and cut token usage
immediately.

**Tier 2:** column-level edges (#2), `body_hash` + graph-diff, `layer`/`domain`
tags.

**Tier 3:** test edges (#4), precomputed `dependents` / impact set (#5),
edge `confidence`, optional cached `summary`.

---

## Constraints to preserve

- **Build stays deterministic / no LLM** (the optional `summary` is the only
  exception, and should be a separate, cached pass — never required for a build).
- **Backward compatible:** additions only; existing node kinds, edge types, ID
  grammar, and `graph.json` shape remain valid.
- **Allowlists must be updated together:** any new node kind / relationship type
  must be added to the `NODE_KINDS` / `REL_TYPES` allowlists in `cpg/store.py`
  (and `cpg/cypher_qa.py`) so Neo4j labels/types and Cypher generation stay aligned.

---

## Implementation surface (when picked up)

| Change | Files |
|---|---|
| New node/edge constructors + IDs (`field_id`, `schema_id`, `column_id`) | `cpg/model.py` |
| Emit `Field`/`Schema`/`Column` nodes, `ACCEPTS`/`RETURNS`/`READS_FIELD`/`READS_COLUMN`, `signature`, `side_effects` | `cpg/backend.py` |
| Resolve field/column placeholders to real nodes; precompute `dependents` | `cpg/resolve.py` |
| Add new kinds/types to allowlists | `cpg/store.py`, `cpg/cypher_qa.py` |
| `body_hash` / graph-diff seeds; optional consume in retrieval | `cpg/retrieve.py`, `cpg/locate.py` |
| Document new ontology | `architecture.md` |
