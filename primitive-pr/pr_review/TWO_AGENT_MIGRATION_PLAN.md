# Migration Plan — Two-Agent Review → Sail Developer Assistant

**Goal:** Replace the *internal* code-review engine in
`sail/services/developer_assistant` (currently the coarse-to-fine per-file
`Indexer → FileReviewer → Aggregator` workflow) with the **two-agent review**
engine from `pr-review/primitive-pr/pr_review` — **without changing the
frontend or any API / SSE / DB contract**.

**Non-goal:** Touching the React frontend, the REST routes, the DB schema, or
the auth/job model. Those stay byte-for-byte the same; only the engine that
*produces findings* changes.

---

## 1. What "two-agent review" actually is

From `primitive-pr/pr_review/two_agent_review.py::run_two_agent_review(cg, src_path, diff_text, complete, depth, budget, max_workers)`:

- **Agent 1 — whole-file pass** (`pr_passes.pass_whole_file`): one LLM call per
  changed file (chunked), given the diff + full file. Finds bugs / secrets /
  breakage / optimizations the change introduces.
- **Agent 2 — dependency / breaking-impact pass**: for each *changed function*
  it builds a prompt containing the changed function + its **dependent caller
  functions** (resolved from a code graph), and asks the LLM which dependents
  the change breaks. Results are enriched (rename detection, exact broken
  callsite lines, provenance) and deterministically synthesized.
- Both agents run concurrently in one `ThreadPoolExecutor`.
- Output: `List[FileReview]`, each with `file_findings`, `dependency_findings`,
  and `dependency_findings_by_function`.

**Critical finding (de-risks the whole migration):** the two-agent path needs
**no Neo4j**. It only needs an *in-memory* `CodeGraph` from
`graph.build_graph(src_path, backend="primitive")` (tree-sitter). Neo4j /
`architecture.py` / `graph_explorer.py` belong to the *other* (impact-chain)
track and are **not** imported by the two-agent path. Verified: no `neo4j`
import in `two_agent_review.py`, `prompt_builder.py`, `pr_passes.py`.

---

## 2. The contract we must preserve (the invariants)

These are consumed by `frontend/.../code_review/ReviewDetailPage.tsx` and
`services/codeReviewApi.ts`. **Do not change any field name or event name.**

### SSE events (`reviews.py::stream_review`, emitted via `emit_event_threadsafe`)
| Event | Payload fields the UI reads |
|---|---|
| `index_done` | `total_files`, `message` |
| `chunk` | `path`, `tokens` |
| `finding` | `path`, `line`, `severity`∈{critical,high,medium,low}, `rule`, `message`, `snippet`, `line_text` |
| `file_done` (live) | `path`, `finding_count`, `critical`, `high`, `medium`, `low` |
| `file_done` (DB replay) | `file_path`, `findings: [{line,line_text,severity,rule,message,snippet}]` |
| `file_skipped` | `path`, `reason` |
| `error` / `workflow_done` / `stream_end` / `ping` | as today |

### DB rows
- **`ReviewFile`**: `{job_id, file_path, language, status, findings_json:[{line,line_text,severity,rule,message,snippet}], tokens_used}`
- **`ReviewResult`**: `{job_id, summary, total_issues, critical, high, medium, low, findings_json, created_at}`
- `ReviewJob.total_files` / `processed_files` / `progress` drive the progress bar.

**Therefore the single hard requirement of the migration is:** the two-agent
engine's `Finding` objects must be mapped to the dict
`{line, line_text, severity, rule, message, snippet}` (plus `path` on events),
grouped per file, and the SSE events + DB rows must be emitted exactly as today.
Everything else is plumbing.

---

## 3. The three gaps between the engines

| Need (two-agent) | Sail provides today | Gap to bridge |
|---|---|---|
| **Local source tree** `src_path` (tree-sitter walk + reading dependent function bodies) | `GitConnector` is **REST-API only** — `get_file_content(path, ref)` per file, no checkout | Must materialize a full snapshot at head SHA (tarball → temp dir) |
| **One unified diff text** (`diff --git`/`+++` headers; `parse_diff` & `_diff_by_file` key off these) | `get_pr_diff`/`get_commit_diff` return `List[DiffEntry]` with per-file `.patch` fragments | Reconstruct a single unified-diff string from `DiffEntry[]` |
| **`complete(system, user) -> str`** callable | `LLM.get_provider(...).generate_completion_response(request)` returning `response.response` | Thin adapter wrapping sail's LLM (mirror `code_review_tool._review_with_*`) |
| **Deps**: `tree-sitter`, `tree-sitter-python/javascript/java/go`, `networkx` | not present in `sail_core` | Add to `sail_core` deps; ship wheels |

---

## 4. Migration steps

### Phase 0 — Vendor the two-agent engine into `sail_core`
Create `packages/sail_core/sail_core/two_agent_review/` and copy **only** the
modules the two-agent path imports (transitively):

**Copy:** `findings.py`, `diff.py`, `graph.py`, `graph_contract.py`,
`impact.py`, `prompt_builder.py`, `pr_passes.py`, `two_agent_review.py`.
*(`pr_passes` imports `impact`; `impact` imports only `diff`+`graph` — still no Neo4j.)*

**Do NOT copy:** `graphify_adapter.py`, `vendor_graph/*`, `neo4j_store.py`,
`architecture.py`, `llm.py`, `review_llm.py`, `github_client.py`,
`synthesis.py`, `graph_explorer.py`, `diagnose_neo4j.py`.

- **Graphify is severed physically, not just flagged off.** *Decision locked:*
  the perfect primitive-pr runs used the primitive (tree-sitter) backend; the
  `vendored`/`graphify` backend was only a testing path that reached into the
  graphify folder, which we do not want. In the copied `graph.py`:
  - delete the import `from .graphify_adapter import try_build_with_graphify`,
  - delete `if backend == "vendored": backend = "graphify"` and the entire
    `if backend in {"graphify", "auto"}: ...` block (graph.py:801–815),
  - `build_graph` then always runs the tree-sitter walk — no graphify code path
    exists and nothing graphify-related is importable.
  Safe because that graphify import was already lazy (inside
  `try_build_with_graphify`); removing the branch leaves the primitive path
  fully intact. Call site stays `build_graph(src_path)` (default backend).
- Add to `sail_core` deps: `networkx>=3.0`, `tree-sitter>=0.25`,
  `tree-sitter-python/javascript/java/go>=0.23`.
- Fix relative imports to the new package location.

### Phase 1 — Source materialization (tarball)
Add to `GitConnector` (and a sensible `VCSConnectorBase` default / SVN
override):
```python
def download_source(self, ref: str, dest_root: str | None = None) -> str:
    # GET /repos/{owner}/{repo}/tarball/{ref} via self._session
    # extract to tempdir (reuse primitive's path-traversal guard for py<3.12)
    # return path to extracted top-level dir
```
Model it on `primitive-pr/.../github_client.py::download_source` but reuse
sail's authenticated `self._session` and SSL settings. Cache by `(owner, repo,
sha)` in a temp dir with a TTL to avoid re-downloading on retries/large repos.
Lifecycle: create early in the run, **delete in `finally`** (job_runner).

### Phase 2 — Unified-diff reconstruction
Add a helper `diff_entries_to_unified(diffs: List[DiffEntry]) -> str`:
```
for d in diffs:
    if d.status == "removed" or not d.patch: continue
    out += f"diff --git a/{d.path} b/{d.path}\n--- a/{d.path}\n+++ b/{d.path}\n{d.patch}\n"
```
This satisfies both `diff.parse_diff` (keys on `+++ b/`, `@@`, `+`/`-`) and
`two_agent_review._diff_by_file` (keys on `diff --git ` and `+++ `). Files where
GitHub omits the patch (very large) simply yield no changed lines — acceptable
(removed files are already skipped today).

### Phase 3 — LLM adapter
```python
def make_complete_fn(llm, agent_config) -> Callable[[str, str], str]:
    provider = agent_config.get("llm_provider", "bedrock")
    model    = agent_config.get("llm_model")
    def complete(system: str, user: str) -> str:
        if provider == "openai":
            req = OpenAiCompletionRequestSchema(prompt=f"{system}\n\n{user}",
                     model=model, max_tokens=4096, temperature=0.1)
        else:
            req = BedrockCompletionRequestSchema(prompt=f"{system}\n\nUser: {user}",
                     model=model, max_tokens=4096, temperature=0.1)
        return llm.generate_completion_response(req).response.strip()
    return complete
```
Mirror `code_review_tool._review_with_openai/_bedrock`. Surface
`LLMRateLimitError` so it becomes a `file_skipped` event (consistent with today).

### Phase 4 — Replace the workflow nodes (keep the Workflow shape)
In `app/services/code_review/`:

- **Keep `IndexerNode`** for `job_type == "full"` (see Phase 8) and to compute
  `total_files`.
- **Add `TwoAgentReviewNode`** that replaces `FileReviewerNode` +
  `AggregatorNode` for `pr`/`commit` jobs:
  1. `src_path = connector.download_source(head_sha or vcs_ref)`
  2. `diff_text = diff_entries_to_unified(connector.get_pr_diff|get_commit_diff(vcs_ref))`
  3. `cg = build_graph(src_path, backend="primitive")`
  4. Emit `index_done {total_files = len(changed_files)}` and a `chunk
     {path, tokens}` per changed file (keeps the progress UI/log alive).
  5. `reviews = run_two_agent_review(cg, src_path, diff_text, complete, depth, budget, max_workers)`
  6. Map → emit → persist (Phase 5), then build `ReviewResult`.
- Register in `job_runner._run_async` instead of `FileReviewer`/`Aggregator`;
  keep `IndexerNode` path for full scans. `set_routing` adjusted accordingly.

**Live-streaming decision (recommend A2):**
- **A1 (simplest):** call `run_two_agent_review` to completion, then loop the
  `FileReview` results emitting `finding`/`file_done`. Downside: UI shows
  nothing until the whole run finishes.
- **A2 (recommended):** add an optional `on_file_done(file_review)` callback to
  `run_two_agent_review` (it already collects futures in a loop — emit per file
  as each completes). Small additive change; preserves the live-stream UX the
  frontend is built around.

### Phase 5 — Finding mapping (`Finding` → sail finding dict)
For every `FileReview`, take `file_findings + dependency_findings` and map each
`Finding`:

- **Re-bucket by `finding.file`** (not by `FileReview.path`). Dependency
  findings point at the *dependent* file/line, and the UI groups by file with
  line-matched snippets — so they must be emitted/persisted under their real
  `file`. Build the per-file `findings_json` from this re-bucketed view.
- `severity`: pass through; map `"info" → "low"` (UI only has 4 levels).
- `rule`: derive a short token from `category`:
  `breaking→"breaking-change"`, `vulnerability→"security"`, `bug→"bug"`,
  `optimization→"performance"`, `suggestion→"suggestion"`.
- `message`: `title` + `explanation` (Agent 1) / impact story (Agent 2).
- `line`: `finding.line` (already the consumer/callsite line for dep findings).
- `line_text`: `finding.evidence` (the exact broken line — UI shows it as the
  highlighted chip).
- `snippet`: build the numbered `►` ±context window — **reuse the existing
  logic in `FileReviewerNode.run` (lines ~311–334)**; extract it to a shared
  helper `build_snippet(file_lines, line_no)`. Read file text from `src_path`
  (already local) instead of the connector.

**Carry the two-agent extras as new *optional* fields** (so the slightly
extended card in Phase 5b can show them). Add to each finding dict, defaulting
to empty/absent:
`agent` (`"file"` | `"dependency"`), `recommendation`, `impact_reason`,
`source_fix_example`, `dependent_fix_example`, `dependent_function`,
`dependent_location` (`"{dependent_file}:{dependent_line}"`),
`changed_function`, `provenance_status`. These ride through the **same**
`finding` SSE event, `ReviewFile.findings_json`, and `/result` flatten — purely
additive, so old fields and old behavior are untouched.

### Phase 5b — Small frontend extension (UI shell unchanged)
*Decision locked:* keep the exact layout — files on the left, findings on the
right, the severity dashboard (critical/high/medium/low counts) on top, the live
stream log while running, and the PDF download button. Only the **finding card**
grows to fit the two-agent context:
- Add the new optional fields to `LiveFinding` / `NormFinding` / `ReviewResult`
  in `codeReviewApi.ts` (all optional — no breakage).
- In `ReviewDetailPage.tsx`, the finding card renders, *when present*:
  - an `agent` chip (`FILE` vs `DEPENDENCY`),
  - for dependency findings: a one-line "breaks `{dependent_function}` at
    `{dependent_location}`" + `impact_reason`,
  - a collapsible "Fix" block (`recommendation`, `source_fix_example`,
    `dependent_fix_example`) — reusing the existing snippet-toggle pattern.
- `report_service.generate_pdf` includes the same optional fields so the PDF
  matches the on-screen card.
- Everything else (header, status badge, progress bar, file list, severity
  dashboard, live `EventSource` handling, cancel/PDF buttons) stays as-is.

### Phase 6 — `job_runner` wiring
- `_run_async`: register the new node(s); everything else (status transitions,
  cancel_event, SSE normalization, error handling) is unchanged.
- Add `src_path` temp-dir cleanup in the `finally` block.
- Cancellation: have the `complete` wrapper check `cancel_event` and short-
  circuit; check `cancel_event` before submitting each agent task in
  `run_two_agent_review` (add the hook). Mirrors current cancel semantics.

### Phase 7 — Config / tuning from `AgentConfig`
- `depth` (default 2), `max_workers` (default 4), `budget`
  (`DEFAULT_BUDGET=12000`, or derive from `max_tokens_per_chunk`).
- Reuse `agent.to_review_config_dict()`; optionally honor
  `system_prompt`/`review_instructions` by prepending to the Agent-1/Agent-2
  system prompts (as `review_code_chunk` does).
- LLM model/provider come from the agent — the adapter handles both
  (primitive used Nova; sail uses its configured Bedrock/OpenAI model).

### Phase 8 — `job_type == "full"` (whole-repo scan, no diff)
Two-agent has no diff for full scans (`run_two_agent_review` returns `[]`).
**Recommendation:** route by `job_type` — keep the **existing** engine
(`FileReviewer`/`Aggregator`) for `full`, use **two-agent** only for
`pr`/`commit` (which is exactly the stated use case). Lowest risk; no behavior
loss for full scans.

### Phase 9 — Tests & validation
- **Unit:** `diff_entries_to_unified` (DiffEntry→unified round-trips through
  `parse_diff`); finding mapping (severity `info→low`, re-bucket by file, rule
  derivation); `make_complete_fn` with a mocked LLM.
- **Parity:** run the PR that is "perfect" in `prompt_test_frontend.py` through
  the sail path and diff the findings against the primitive output. Goal: same
  breaking/dependency findings surface.
- **Contract:** assert emitted `finding`/`file_done` payloads contain exactly
  the fields in §2 (frontend untouched, so this is the guardrail).
- **Regression:** full-scan job still works via the old engine.

---

## 5. Module-by-module summary

| Sail file | Change |
|---|---|
| `packages/sail_core/.../two_agent_review/` (new) | Vendored engine subset (Phase 0) |
| `packages/sail_core/.../connectors/git_connector.py` | + `download_source(ref)` (Phase 1) |
| `packages/sail_core/.../connectors/base.py` (opt) | default/abstract `download_source` |
| `app/services/code_review/two_agent_runner.py` (new) | `diff_entries_to_unified`, `make_complete_fn`, finding mapping, snippet helper (Phases 2,3,5) |
| `app/services/code_review/review_nodes.py` | + `TwoAgentReviewNode`; extract `build_snippet` helper |
| `app/services/code_review/job_runner.py` | Register new node, route by `job_type`, temp cleanup (Phases 6,8) |
| `app/models/code_review/agent_config.py` (opt) | + `depth`/`max_workers`/`budget` knobs (Phase 7) |
| `app/services/code_review/report_service.py` | render new optional finding fields in PDF (Phase 5b) |
| `frontend/.../codeReviewApi.ts` | + optional fields on `LiveFinding`/`NormFinding`/`ReviewResult` (Phase 5b) |
| `frontend/.../code_review/ReviewDetailPage.tsx` | extend **finding card only** with agent chip + impact + collapsible fix (Phase 5b) |
| **UI shell** (layout, file list, severity dashboard, live stream, PDF button) | **UNCHANGED** |
| **REST routes, SSE event names, DB schema** | **UNCHANGED** (new finding fields are additive) |

---

## 6. Decisions
**Locked:**
- **Graph backend = primitive (tree-sitter); graphify never used** → full parity with the perfect runs.
- **One engine for `pr` + `commit`** (only the diff source differs).
- **UI shell unchanged**; only the finding card is extended (Phase 5b). Severity dashboard, live stream, and PDF button all preserved.

**Still to confirm before coding:**
1. **Live streaming**: A1 (batch) vs **A2 (per-file callback)** — recommend A2 since the UI is built for live streaming.
2. **Tarball cost**: cache by `(repo, sha)` + TTL; cap repo size?
3. **Heavy deps** (`tree-sitter*`) into `sail_core` — confirm acceptable for the deploy/build pipeline.
4. **`full` scans**: keep old engine (recommended) vs Agent-1-only over all files.
5. **Language coverage**: graph build is full for Python, generic for JS/Java/Go; other languages → Agent 1 still works, Agent 2 yields nothing (acceptable).
