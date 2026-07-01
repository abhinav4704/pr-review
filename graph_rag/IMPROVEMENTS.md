# Improvements & Next Steps

> Running backlog of known gaps and worthwhile improvements, by area. Current
> as-built state is in [`STATUS.md`](STATUS.md). Last updated 2026-06-30.

## 0. Validate what's built (do this first)
Phases 2–3 are code-complete but **never run end-to-end**. Before building more:
1. Start Neo4j, `index` a small repo.
2. `cli semantic` on a `--limit 5` smoke test → inspect `identity` + `keywords/tags/concepts` in Neo4j.
3. `cli semantic --flows --limit 5` → inspect the typed flows + grounded `flow_references`.
4. `cli embed --limit 20` → confirm `code_embedding` index builds and vectors land.
5. Run the vector-search Cypher in RUN.md against a query vector.

## 1. Retrieval + agent loop (the next big build)
The hybrid retriever the flow we designed needs:
- **Candidate search:** vector (`code_embedding`) ∥ BM25 over `identity_keywords/tags/concepts` + names/signatures.
- **Rank / rerank:** fuse vector + keyword scores; optional LLM rerank.
- **Prune #1 (LLM):** drop candidates out of scope for the question. *(memory: dedup/prune is LLM-driven, not Python set-logic.)*
- **Graph expansion:** blast radius (CALLS in/out), parent class/package, READS/WRITES — pull neighbors' identities + flows.
- **Prune #2 (LLM):** drop unnecessary neighbors.
- **Context pack:** target node → full source; neighbors → signature + identity (no body) — the token win.
- **Output:** feed generation (Job 2) or analysis (security/vuln/impact).
- Surface as `cli ask "<question>"` and/or a library API.

## 2. Identities
- **Template trivial members.** Getters/setters/`__init__` shouldn't cost an LLM call — fill from a template. Biggest cost win, no quality loss.
- **Add `side_effects` + `preconditions` to the identity schema.** High-value for grounded generation (Job 2); the original `SEMANTIC_LAYER.md` specced them. Promote just these two, not the whole heavy schema.
- **Enclosing-context tension.** Bottom-up means a function's identity is generated before its class/package identity exists. If class identities feel thin, add a cheap top-down refinement pass (~2× cost) — only if quality demands it.
- (Deferred by choice: model tiering — model comes from `.env`.)

## 3. Implementation flows
- **Class `Behavior` artifact.** `generate_flows` only targets functions. The design doc gives classes a Behavior (workflow → major_operations → state_changes). Classes currently have identity but no behavioral view — main gap.
- **Per-step `condition`.** Steps are a linear sequence; add an optional "runs when X" to capture branches/decision points without full control-flow.
- **Eager flows for hot nodes.** Optionally pre-generate flows for high-fan-in / central functions instead of fully lazy.

## 4. Embeddings
- **Code-tuned model** if retrieval quality is weak: `voyage-code-3` (needs key) or a larger local model. Default is `all-MiniLM-L6-v2` (general, 384-dim).
- **Hypothetical-question embeddings** on high-value nodes (HyDE-style) — embed "what question would this answer" for better recall.
- **Embed flows too**, not just identities, for "how is X implemented" queries.

## 5. Structural graph (from the earlier backlog)
- **AUTOWIRED** — deterministic Java Spring DI edges (constructor/field/param injection → resolved type). Priority 2 in the earlier summary.
- **`@PreAuthorize` double-tags** as REQUIRES_AUTH + ENFORCES_POLICY (name contains "auth"). Tighten the substring if precision matters.
- **scip-java** — precise Java CALLS. Blocked: needs Coursier + scip-java + a buildable Maven/Gradle project (none present). Revisit when a real Java repo is available.
- **`Module` from build files** — currently derived from package/path; could also read pom.xml/package.json/Cargo.toml.
- **Role classification** — rules are now a table (`_CLASS_ROLE_RULES`) + diagnostics; extend the table as needed.
- **RE_EXPORTS / JS-TS extractor** — when TypeScript/JavaScript indexing is in scope (unblocks the frontend→backend `CALLS_API` leg).

## 6. Freshness & ops
- **Incremental re-index** — git post-commit hook: re-run Stages 1–2b on changed files, re-`semantic`/`embed` only nodes whose `body_hash` changed, patch edges.
- **Exporter** — dump a repo subgraph → `graph.json` / `GRAPH_REPORT.md` / `graph.html`.
