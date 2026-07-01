# Next TODOs — Codebase Analysis Layer (Phase 4)

> Semantic (Phase 2) and retrieval (Phase 3) are built and validated — see
> [`STATUS.md`](STATUS.md) and [`SEMANTIC_LAYER.md`](SEMANTIC_LAYER.md). This is the design
> for the **next** layer: full codebase health analysis (security, correctness, reliability,
> design, optimization, coding standards) with auto-fixable, re-runnable findings.
> **Design/ideation only — not yet implemented** (as of 2026-07-01).

## Goal

Not RAG — a full codebase review: every function gets analyzed for vulnerabilities, bugs,
design issues, and standards violations. No default triage/severity filtering — everything is
analyzed and reported; prioritization happens on the full output, not before. Every finding
carries a blast-radius/propagation chain (who's affected, not just the origin). Output must be
precise enough for an automated agent to auto-fix. Re-running after a code change should only
re-analyze what changed (cached, incremental). Long-term this feeds the same graph used for RAG
and code generation — one graph, one brain, for review + Q&A + generation.

## Three-Phase Architecture

| Phase | What runs | LLM? | Ordering needed? |
|---|---|---|---|
| **A — per-function analysis** | identity + flow of self, direct dependents, direct dependees (+ full source only if the model reports `needs_source`) | Yes, once per function | No — fully parallel |
| **B — blast-radius propagation** | graph traversal over `CALLS` edges (visited-set, so cycles are a non-issue) | No — pure Cypher/graph algorithm | No |
| **C — severity reconciliation** | finding + blast-radius number as input, batchable (many findings per call) | Yes, but cheap/batched | No |

**Why this shape:** Phase A only needs *identity* (already built by Phase 2, static/stable) for
neighbor context — never another node's in-progress findings. That single decision removes the
sequential-ordering problem and the cycle problem for the expensive step. Phase B is deterministic
graph math, trivially handles cycles via a visited-set. Phase C is small and batchable since it
only needs the finding text + a number, not full node context.

## Key Design Decisions

- **Function-level granularity, not class-level.** "Full codebase review" means every function
  needs its own precise finding for auto-fix to work; bundling methods into one class-level call
  would blur per-function fix instructions.
- **Background/async job model.** Phase A being slow across a large repo is acceptable because
  (a) it runs as a background task, not real-time, and (b) it's still cheaper over time than an
  approach with no persistent graph/cache (e.g., an assistant re-reading the whole repo context
  on every query).
- **Blast radius is a signal, not a rule.** Phase C feeds blast-radius as *input* to the LLM's
  severity judgment — it is not a deterministic "high fan-in = critical" upgrade. This avoids
  false-critical inflation on popular utility/hub functions (loggers, DB session helpers, etc.).
- **Cycles are judged by the LLM, not resolved via SCC/Tarjan's.** Since Phase A doesn't need
  topological ordering at all (context = stable identity, not in-progress findings), a cyclic
  pair of functions is a non-issue for analysis — the LLM just uses whatever identity is
  available for each side.

## Open Risks & Refinements

### 1. Transitive cache invalidation — confirmed bounded to hop 1
Verified against `semantic.py`'s `_function_identity_prompt`: a function's identity is built
from its own signature/docstring/source plus only the **names** of callees/callers/reads/writes
— never their identity text or internals. So:
- If callee C changes internally, only C's identity regenerates, and only C's *direct* callers
  (e.g., B) need finding re-analysis (their Phase-A context embedded C's identity text).
- It does **not** cascade further (e.g., to A, which calls B) because B's own identity never
  changes as a result (it's shallow/name-based, unaffected by C's internal changes).
- **Invalidation rule:** when a node's identity changes, mark all direct callers' findings stale
  — one hop only, no deeper transitive walk needed.
- **Caveat:** this bound holds only while identity generation stays name-based/shallow. If
  function identity is ever enriched with deeper callee semantics, re-verify this assumption.

### 2. Duplicate findings across a call chain — real risk, needs mitigation
Since Phase A gives a node its dependees' full identity text, the LLM can "rediscover" a
dependee's issue and report it again on the caller — e.g., a root cause in D could surface as 4
separate findings across an A→B→C→D chain, each with a different ID (the ID hash includes `fqn`,
so duplicates won't auto-merge).
- **Primary fix:** explicit prompt rule — only report a finding if the problematic code is
  physically inside *this* function's own body. Dependency issues are linked via blast-radius
  propagation (Phase B), never re-reported as a new finding.
- **Safety net:** embedding-similarity check between findings connected by a `CALLS` edge, to
  catch cases where the LLM didn't follow the scope rule. Reuses the existing embedding
  pipeline — no new infra needed.

### 3. LLM severity consistency
Severity is LLM-judged, not rule-based, so give it a lightweight rubric in the prompt (e.g.,
"high blast radius + security category → lean toward escalation, but use judgment") rather than
leaving it fully open-ended, to reduce run-to-run variance. Low temperature recommended for
Phase C.

### 4. Feedback loop — not yet designed
Needs a concrete mechanism: e.g., track whether a finding was later auto-fixed vs. dismissed,
and feed that back into prompt tuning / reporting metrics over time.

## What's Genuinely Good About This Design (keep)

- Identity + flow instead of raw source for neighbor context — real token efficiency, not
  common in existing tools.
- Repo-scoped, cached, incremental design matches how production static-analysis infra actually
  works (index once, re-analyze only deltas).
- Graph-native blast radius / propagation chains — no mainstream tool (SonarQube, Semgrep,
  CodeQL) has this; a genuine differentiator.
- Schema-first LLM contracts (same "contract first, not prompt first" rule as
  [`SEMANTIC_LAYER.md`](SEMANTIC_LAYER.md)) avoid prompt drift.

## Next Steps

1. Confirm Phase 2B (`semantic --flows`) has been run end-to-end on the target repo — the
   analysis layer depends on Implementation Flow being populated, not just Identity.
2. Design the finding schema (category/subcategory/severity/confidence/fix_instruction/
   needs_source/origin) with a discriminator in the ID (e.g., `start_line`) to avoid collisions
   when a function has two distinct findings of the same category+subcategory.
3. Build `evidence.py` → `security_scanner.py` → `node_analyzer.py` (Phase A) →
   `report_builder.py` (Phase B + C) → CLI/webapp wiring (`analyze` subcommand,
   `/api/analyze/{repo}` endpoints, `analyze.html` dashboard).
