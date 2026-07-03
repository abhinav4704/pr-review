# Codebase Analyzer — Plan (working doc, persisted so nothing is lost on context compaction)

> This file is the durable record of the "Codebase Analyzer" design: the original full plan
> (as drafted), plus every amendment agreed on in review. Treat the **Amendments** section as
> the current source of truth where it conflicts with the original plan below it — the original
> is kept verbatim for reference, not because it's still 100% final.

---

## Amendments (session of 2026-07-03 — read this section first)

### A1. Taint sources — "things that come from the user" (scope definition)
This defines what counts as **attacker/user-controlled input** — the seed points Layer 3's taint
transfer functions must tag as tainted at the origin, and the only thing Stage 1's taint/authz
passes should treat as "untrusted." Scope note: this taxonomy applies to the **security/taint
passes only** (Stage 1 taint composition + authz candidates). It does **not** restrict the
architecture/layering check (A2 below), which is repo-wide regardless of user input, and it does
**not** gate Agent 1's general correctness bugs, which aren't taint-conditioned.

**In scope (tag as tainted at the source):**
- HTTP request data reaching an `Endpoint`-handler function (the existing `Endpoint`/`EXPOSES`
  nodes/edges already mark exactly these functions):
  - Query string parameters
  - Path/route parameters (e.g. the `{id}` in `/orders/{id}`)
  - Request body fields (JSON/form/multipart)
  - Headers (including `Authorization`, `User-Agent`, `Referer`, custom headers)
  - Cookies
  - Uploaded file **contents** *and* **filename/metadata** (filename is classic path-traversal
    payload — don't tag only the content stream and miss the name)
- Message-queue / event-consumer payloads (`CONSUMES_EVENT` handlers) — same treatment as an
  Endpoint param: the producer may itself be external-facing.
- Second-order sources: any value **read** from a table/field that was **written** from one of
  the sources above elsewhere (this is already the plan's "stored/second-order taint" finding —
  cross-reference, don't re-derive).

**Deliberately out of scope for v1 (name it, don't silently skip it):**
- CLI args / environment variables / config values, unless a concrete path shows them as
  user-editable (e.g. a web-exposed settings page writes them). Default: not a source.
- Anything requiring runtime knowledge to classify as attacker-reachable — if it's not
  reachable via a modeled `Endpoint`/`CONSUMES_EVENT` edge, it's not a source, full stop; don't
  guess reachability from the LLM's intuition.

**Where this plugs in:** Layer 3's per-function "taint transfer function" tagging (`taint.py`)
seeds tainted params by checking "is this function's owning node a `component_role ==
endpoint_handler` (or an event-consumer equivalent), and if so, which of its `param_names` map
to the categories above." Everything downstream (taint composition, stored-taint, authz
candidates) inherits from this one seed step — get this list right once, don't re-litigate it
per-pass.

### A2. Architecture / layering check (new Stage 1 pass)
User's original idea: start from a function with no caller, walk the whole call path upward/
downward, analyze the entire path for bad architecture, decide whether to split on branches.

**Recommended implementation (supersedes literal path-walking):**
- **Root anchor:** don't use raw `fan_in == 0` — it conflates real entry points, dead code, and
  false roots from Java's heuristic/INFERRED `CALLS` resolution (a real callee can look
  caller-less just because the resolver missed the edge). Use `component_role ==
  "endpoint_handler"` instead — **confirmed already computed today**, not just a schema field:
  `_classify_roles()` in `graph_rag/pipeline.py` (~lines 264–321) tags any `Function` with an
  `EXPOSES` edge as `endpoint_handler` at HIGH confidence, and every other function inherits its
  owning class's role (`controller`/`service`/`repository`/`entity`/`config`/`util`) from
  name/package/annotation rules.
- **Mechanism: reject full path enumeration.** The call graph is a DAG (often with cycles from
  recursion), not a tree — enumerating every root-to-leaf path re-walks shared utility functions
  once per path that reaches them and blows up combinatorially (the plan's own "god-node guard"
  already names this exact failure mode for hop-2 fan-out). Instead: a **local edge-level
  check** — for every `CALLS` edge `A→B`, look up `(component_role(A), component_role(B))`
  against an allowed-transition table (`controller→service` fine, `controller→repository`
  flagged, `repository→controller` flagged, `util`/`config` callable from anywhere). Violations
  compose automatically across any depth without ever materializing a path. Same
  reuse-the-traversal principle the plan already applies to taint — not a new phase/system.
  A bounded "longest chain to a terminal role" metric (single graph query) covers the
  god-path/maintainability-depth signal without path-string enumeration.
- **Cycles:** guard with a visited-set (same as Stage 3's blast-radius BFS already uses), *and*
  report a cycle that crosses a layer boundary as its own finding type
  ("circular architectural dependency") rather than silently stepping around it.
- **Open problem — whose layering convention is "correct":** a hardcoded transition table will
  false-positive on repos with different conventions. Fix: **one cached LLM call per repo**
  inferring the repo's actual layering convention from observed role-transition edge counts —
  same cost shape as the existing sanitizer-tag design (one-time, cached, reused by every future
  sweep). This is effectively pulling forward a slice of `ARCHITECTURE.md`'s still-unbuilt
  Stage 7 ("Architecture": Community + Convention via GDS Leiden + LLM) as a targeted
  deterministic pass, not the full community-detection system.
- **Where it fits:** new Stage 1 pass (deterministic, no LLM in the traversal) alongside the
  existing authz/concurrency/perf flags. New finding types: `layering_violation`,
  `circular_architectural_dependency`. Feeds the existing Stage 3 blast-radius + severity
  pipeline unchanged — not a new stage, not a new agent.

### A3. Review findings from critique pass (apply before implementation)
1. **Relabel "proof-grade" for Stage 1 taint.** The plan's own §11.3 admits coarse/over-tainting
   taint — that's high-recall/medium-precision, not proof-grade. Use
   `graph_proven` / `llm_judged` / `llm_flagged_candidate` as the `source` tag (already proposed
   in §11.1) consistently, and stop calling taint composition "proof-grade" elsewhere in the doc.
2. **Add a hard routing gate before Agent 2**, not just the god-node guard: run Agent 2 only for
   endpoint-reachable functions, sink-adjacent functions, and top-centrality-percentile functions.
   Without this, Stage 2 cost is closer to superlinear than the plan implies on a real repo.
3. **Skip Stage 3's qualify step when a finding already carries Agent 2's caller judgment** —
   both ask "does this caller's input matter" and doing it twice wastes calls for zero accuracy
   gain. Keep qualify for Stage-1-only and Agent-1-only findings, where it's still new work.
4. **Strengthen the finding-ID scheme** beyond `hash(owning_fqn + category + subcategory +
   line_start)` — add a normalized-evidence-snippet hash so formatter/refactor line churn doesn't
   fracture suppression history.
5. **Version the cache key**, not just `body_hash`/neighbor-hashes — add
   `transfer_version + sink_rules_version + sanitizer_model_version + prompt_version +
   severity_formula_version` so a logic upgrade doesn't silently leave stale findings cached
   forever.
6. **Cap confidence on concurrency/perf-smell findings** unless two independent signals agree
   (e.g. race = read-write pattern + shared mutable target + cross-request reachability) — these
   two classes are named in the plan itself as the noisiest.
7. **Add operational KPIs to the eval harness**, not just detection rate: precision-at-top-20,
   time-to-first-actionable-finding, suppression-reuse rate, reopen-rate after suppression.
   Detection metrics alone don't predict whether a team keeps running the tool.
8. Priority order for the write-up in §11 stands: **11.1 (severity vs confidence split), 11.2
   (suppression/baseline), 11.4 (eval grading its own homework) before any new bug class; 11.3
   (name the taint precision level) decide now, don't discover later; 11.5 (clustering) can wait.**

---

## Original plan (verbatim, as drafted)

# Codebase Analyzer — Full Plan

> Whole-repo static + LLM analysis over the Codebase Brain graph. Finds vulnerabilities, correctness bugs, breakage, and design issues across an entire codebase — not a diff.
>
> **One thesis:** the graph is the *retriever*, the LLM is the *analyst*, and everything the graph can prove never touches a model. Accuracy and token-efficiency come from the same move — deterministic-first, LLM only where judgment is genuinely required.

---

## 0. The core idea in one paragraph
Grep-style agents (Copilot CLI) do *search-time understanding* — they re-discover the codebase on every run. We do *pre-computed understanding* — build the graph once, then serve each analysis call a small bundle of distilled facts instead of raw files. Copilot's LLM must parse the codebase itself; ours reads a pre-chewed context pack plus facts (taint, reachability) that no amount of reading can recover. Fewer tokens **and** higher recall on the classes that matter, from the same source: **we compute what the code doesn't say out loud.**

---

## 1. What already exists (the base — do not rebuild)

| Layer | What | Status |
|---|---|---|
| **Layer 0** | Structural graph — nodes + edges, **both directions** (`CALLS`, `READS`, `WRITES`, `CONTAINS`, …). Better than the primitive-pr graph. | ✅ built |
| **Layer 1** | Identities + implementation flows per function | ✅ built |
| **Layer 2** | Embeddings + hybrid retrieval (`retrieval.py`) | ✅ built |
| **Reuse from primitive-pr** | `prompt_builder._render_node_block` (line-numbered source rendering), path-mismatch/fallback helpers, `findings.py` model, `parse_findings`, `dedupe`, provenance-gate discipline | lift as-is / adapt |

**Reused, but rewritten:** primitive-pr's `build_prompts_by_function` and `build_changed_file_chains` are **diff-native** — they assume "changed function" and walk the *caller* direction only. Whole-repo has no diff and needs **both directions**. Keep the *renderer*, rewrite the *selection logic*.

---

## 2. What's missing (the only genuinely new deterministic work)

### Layer 3 — facts (mostly tree-sitter, near-zero LLM, ~1 week)

- **Taint transfer functions** (`taint.py`, per function, no LLM):
  - Records: `param 0 → arg 1 of callee X`, `param 2 → return value`, `param 1 dies here`.
  - Stored as a Function node property. Cached by `body_hash`.
  - This turns *reachability* into *actual taint*. A `CALLS` edge means control reaches; a transfer function means **data** reaches. Without this, taint is just "who can reach a sink" — which over-counts every endpoint into every sink.
- **Sink tags** — deterministic allowlist: `execute`, `eval`, `subprocess`, `pickle.loads`, `yaml.load`, f-string-into-query, `open`, deserialize, … Regex/AST match. Exact-pattern territory — regex is correct here, not a smell.
- **Sanitizer tags** — one cached LLM call **per candidate only** (functions whose name/flow suggests validation — not all 163):
  - Shape: `{sql_injection: [0], xss: []}` — **per vuln class, per param index.** Not a boolean.
  - A `sql_escape()` sanitizes for SQLi and does nothing for XSS; a boolean tag would lie.

**Exit test for Layer 3:** hand-seed 5 known taint paths at varying depth (2–4 hops), one with a real sanitizer on the path. Cypher composition must find 4 and skip the sanitized one.

---

## 3. The flow — four stages

```
Stage 0  build base ─────────────────────► queryable brain (both directions)
Stage 1  Cypher sweep ──► findings (proof-grade) + flags (to judge)   [NO LLM]
Stage 2  ┌ Agent 1: correctness  (callee ctx, raw code)               [LLM]
         └ Agent 2: impact/breakage (caller ctx, identity/flow)       [LLM]
Stage 3  blast (Cypher) + qualify (1 LLM/finding) + severity (formula) [MOSTLY NO LLM]
```

**Stages are time (execution order). Agents are task-separation (who asks what).** They're different axes — the two-agent split lives *inside* Stage 2. Stage 3 is not a third agent; it's the scoreboard.

---

### Stage 0 — Build the base (deterministic, once)

- Graph + Layer 1 + Layer 3. Nothing analyzed yet — just made *knowable*.
- Incremental: `body_hash` changes → recompute only that node's Layer 3 facts.

---

### Stage 1 — Deterministic sweep (Cypher, no LLM, runs first)
The cheap, high-confidence, exact leg. Runs **before** any LLM so it can *target* Stage 2. Produces two output types:

- **Findings** (proof-grade, emitted directly):
  - **Taint composition** → injection family (SQLi, command, path traversal, SSRF). Composes Layer-3 transfer functions along `CALLS` from an Endpoint param to a sink with no matching sanitizer on the path. Multi-hop. This is your headline edge over Copilot.
  - **Stored / second-order taint** → table that is `WRITES`-reachable from an Endpoint param **and** `READS`-flows to a sink elsewhere. Bridges the write-tainted/read-trusted gap that value-taint can't cross. Catches stored XSS, second-order SQLi.
- **Flags** (candidates Stage 2 must judge — the graph narrows *where to look hard*):
  - **Authz candidates** → every Endpoint that `READS`/`WRITES` a user-scoped table keyed by request input. (The `/orders/{id}` no-ownership-check class.)
  - **Concurrency candidates** → functions that READ then WRITE the same shared resource with no lock/transaction between. (RMW / TOCTOU.)
  - **Perf smells** → DB `CALLS` inside a loop (N+1), nested loops over the same collection (O(n²)). Pattern match — finds the *smell*, says nothing about actual latency.

**Why first:** it's free, exact, and it converts Stage 2 from "read a file, hope you notice" into "answer this specific question the graph already flagged." Targeting > ambient observation.

---

### Stage 2 — LLM analysis (the expensive core, two agents)
**Unit of work:** a function + its bundle. The two-agent split is deliberate — separated tasks beat one overloaded call. This is the same principle as targeting, applied to agent design.

#### Agent 1 — Correctness ("is this code wrong on its own terms?")

- **Context:** self (raw code) + **callees** (raw — correctness needs detail: "does it trust what it gets back?") + Layer-3 facts about self (e.g. "param 0 is Endpoint-reachable, unsanitized").
- **Catches (evidence inside the code it reads):** intra-function bugs, null deref, unhandled empty/error, bad error handling, local resource misuse (`close()` then `send()` in one function), local RMW races.
- **Targeted prompt lines, not one omnibus "find bugs":** flagged concurrency candidates get an explicit "flag read-modify-write on shared state without a lock" line; resource-flagged functions get "flag use-after-release." Separate questions → separate attention. *(Falsifiable — see eval §6. If omnibus catches everything the targeted prompts do, drop the targeting. My bet: it won't, and the extra prompt lines cost ~nothing.)*

#### Agent 2 — Impact / breakage ("if it's wrong, who bleeds?")

- **Context:** self + **callers** (identity/flow — impact only needs the *shape*, not full bodies) + Layer-3 facts.
- This is primitive-pr's Agent 2, generalized off the diff. Escalates a caller to **raw body** only when a resource/handle/lock is passed across the edge, or when the model reports `needs_source` — bounded, targeted escalation, never blanket reading.
- **Catches:** cross-function breakage, cross-file resource-lifecycle bugs (double-release across a `CALLS` edge), broken caller/callee contracts.

**Directions are two roles, not two piles.** Callee-context = "is this correct." Caller-context = "does its bug matter." Don't collapse them into one undifferentiated neighbor dump — that pays double tokens to make the question fuzzier. Callees raw, callers as flow.

#### Bundle shape (per function)

```
self          → full raw source
callees       → raw source            (Agent 1 / correctness)
callers        → identity + flow      (Agent 2 / impact; escalate to raw on resource-pass signal)
Layer-3 facts → tainted params + origin, sink tags
flags          → targeted questions routed from Stage 1
```

**God-node guard:** if a node's hop-2 fan-out exceeds a degree threshold (~40), hop-2 stays identity/flow only — never all-raw. Prevents the 100+ function bundle on hubs. (A PR never hit this because a diff rarely touches a hub; whole-repo hits it constantly.)

#### Finding attribution + dedup (structural, not textual)

- Every finding is attributed to the function whose **body physically contains** the bug — the *owning-fqn* — even when surfaced from a caller's bundle.
- **Finding ID = `hash(owning_fqn + category + subcategory + line_start)`.** Fix this now, before findings exist to migrate. Two distinct bugs in one function can't collide; the same bug surfaced from two overlapping bundles collapses by lookup.
- Prompt rule ("only report bugs in this function's own body") handles most duplication — **but prompt rules leak ~10–20%.** The owning-fqn ID is a *free* deterministic safety net that catches the leaks. Keep both.

---

### Stage 3 — Propagate + score (the scoreboard, mostly no LLM)
**Stage 3 finds nothing new. It sorts the pile Stages 1–2 produced.** The agents work one bug at a time through a keyhole; they can't rank, because ranking is *relative* and needs all findings to exist first. So this comes last, and only the whole-pile view can do it.

Three questions per finding:

1. **"Who does this bug hurt?"** → **blast radius.** Pure Cypher walk over callers. No LLM. *"SQL injection in `fetch_rows`, reachable by 12 functions."*
2. **"Does it *actually* hurt all 12?"** → **qualify.** One batched LLM call per finding: *"here are the 12 callers, which pass genuinely dangerous input vs. hardcoded/guarded args?"* → 3 real, 9 shielded. Cached by `(finding_id, caller_body_hash)`. *(Open decision — §7: skip this and rank on raw blast count if the call isn't worth it.)*
3. **"How bad, as a number?"** → **severity.** Pure formula, zero LLM: `severity = f(class_base, endpoint_reachable, qualified_blast_count, writes_sensitive)` Reproducible by construction, explainable, no run-to-run drift. This deletes the old judgment-based Phase C entirely.

Output: the pile, sorted.

```
CRITICAL 9.2  SQL injection in fetch_rows      (3 exploitable paths)
CRITICAL 8.8  auth missing on /orders
HIGH     7.1  unhandled empty in run_search
...
LOW      2.0  dead code in old_helper
```

---

## 4. Worked example (one chain, all four stages)

```
# api.py
@app.post("/search")
def search_endpoint(query: str):        # Endpoint, param 0 = untrusted
    return run_search(query)

# service.py
def run_search(term):                   # B
    cleaned = normalize(term)           # normalize only lowercases — NOT a sanitizer
    return fetch_rows(cleaned)

# db.py
def fetch_rows(filter_str):             # C
    sql = f"SELECT * FROM docs WHERE name = '{filter_str}'"
    return conn.execute(sql)            # SINK — injection
```

- **Layer 3:** `search_endpoint`: `param0 → arg0 run_search`. `run_search`: `param0 → normalize → cleaned → arg0 fetch_rows`. `fetch_rows`: `param0 → f-string → conn.execute` (sink). `normalize` sanitizer tag → `{sql_injection: []}` (does nothing).
- **Stage 1:** composes the chain → **SQL_INJECTION at `db.py:fetch_rows:3`, path `search_endpoint → run_search → fetch_rows`.** Proof-grade, no LLM. *Why Copilot misses it: it read all 3 functions — at different times. `fetch_rows` alone looks like an internal helper. The bug lives in the composition, not in any node.*
- **Stage 2 / Agent 1** on `run_search`, separately, catches a different bug taint can't see: `return rows[0]` crashes on empty result → `{correctness/unhandled_empty, line 3, fix: guard len==0}`. And the prompt rule stops it re-reporting the injection here — that's `fetch_rows`'s body, linked via blast radius, not re-filed.
- **Stage 3:** blast → callers of `fetch_rows` = `run_search`, `export_report`, `admin_cleanup`. Qualify → `run_search` yes (user input), `export_report` no (hardcoded `"status='done'"`), `admin_cleanup` conditional (config input). Severity → `base(sqli=9) × endpoint_reachable(1.0) × blast(2/3) → CRITICAL`. Same number every run.

**Token math, Agent 1 on `fetch_rows`:** ~1.1k tokens once, cached (3 lines source + 2 caller identities + 1 taint fact + prompt). Copilot's agent equivalent: reads `db.py` + greps + opens `service.py` + `api.py` full bodies ≈ 3–5k, and forgets it all next run.

---

## 5. What you WILL and WON'T find (read this twice)
A vague answer here is dangerous — you'll trust a clean report you shouldn't. So, blunt:

### WILL find — high confidence

- **Injection family, including multi-hop** — SQLi, command, path traversal, SSRF. *Better than Copilot.* Your edge.
- **Second-order / stored injection** — storage-bridge query. Copilot's view structurally can't.
- **Intra-function correctness** — null deref, unhandled empty/error, resource leak *within one function*, use-after-close *within one function*.
- **Missing authorization on endpoints** — the authz targeting pass.
- **Cross-function breakage** — Agent 2 + blast radius. primitive-pr's proven strength.
- **Design / structural smells** — god nodes, tight coupling, circular deps. Graph sees directly.

### WILL find — medium confidence (real FP/FN rate — must eval, must label)

- **RMW / TOCTOU races** — pre-filter + prompt. Mediocre recall, some FPs. Bounded win, not a guarantee.
- **Performance smells** — N+1, O(n²). Finds the *pattern*, not latency.
- **Cross-file resource-lifecycle bugs** — only if Agent 2 escalates to raw bodies on the right nodes. Leaky.

### WON'T find — structural blind spots. State these as "NOT ANALYZED" in the report.

- **Deadlocks, lock-ordering, async scheduling** — the model has no notion of "simultaneous" or "ordering." Not a recall problem — the info isn't in a static graph.
- **Value-dependent state bugs across branches** — "fails only when object in state X via path P" — needs a CFG (deferred Phase 6). Not built.
- **Business-logic flaws that aren't access-control** — "discount applies twice because rules interact wrong." No sink, no missing-check pattern. LLM *might* stumble on it; no systematic coverage.
- **Actual latency / runtime perf** — needs profiling. Static finds smells, never p99.
- **Anything requiring runtime values** — "crashes when config.timeout < 0." Not statically decidable.

### The honesty rule for the report

- On WILL-find classes → clean means *probably clean*.
- On WON'T-find classes → clean means **"not analyzed,"** never "no bugs." A green checkmark next to concurrency is a liability, not a feature.

**The through-line:** you find everything whose *evidence is written down somewhere the graph can reach* — a function body, an edge, a taint path, a storage round-trip. You miss everything whose evidence lives in *runtime* — interleavings, value-dependent states, timing. That boundary is **information availability, not model IQ.** No amount of "read harder" recovers a fact that was never in the code. Own the boundary out loud → trustworthy. Hide it → the first production deadlock burns your credibility.

---

## 6. Eval harness — non-negotiable, build after Stage 1
Six passes = six ways to be confidently wrong without this. One anecdotal query is not validation (you know this — the CSR GraphRAG got a 15-question suite and 93%; replicate that rigor).

- **Seeded branch, 12–15 bugs:**
  - 2 multi-hop injections (one via a **dynamically-registered callback** — no lexical trail for a grep agent to follow)
  - 1 stored XSS (write in file A, read+render in file B, **no direct call edge**)
  - 3 intra-fn logic bugs · 2 auth-missing · 1 double-release cross-file · 1 RMW race · 1 bad error handling
- **Score 3 axes vs. Copilot CLI agent (full-repo review task):** detection rate · false positives · **total tokens.**
- **Per-pass precision/recall.** No aggregate "full analysis" score hiding a pass that's 40% FP.

**Falsifiable bets this settles:**

- *6-bugs-in-one-file:* targeted prompts vs. omnibus. If omnibus catches all 6, targeting is dead weight — drop it.
- *Stored XSS:* if `self + callers + callees` raw reading catches it **without** the storage-bridge query → I'm wrong, want the transcript, will update hard.
- *Multi-hop injection:* you win vs. Copilot unless the chain is short enough to co-occur in its context. If Copilot's exhaustive sweep catches all chains → your moat is **efficiency + run stability only**, not accuracy. Important either way — know which game you're winning.

---

## 7. Open decisions (flagged, your call)

- **Stage 3 qualify step** — worth the per-finding LLM call, or rank on raw blast count? Precision vs. cost. Cheap to A/B on the seeded branch: does qualify meaningfully re-order the top 20?
- **Model cost tiering** (cheap triage → escalate to strong) — on hold; needs a decision on *where* to apply it given the "no default triage" rule.
- **Java** — `CALLS` is heuristic/INFERRED there, and taint extraction is messier than Python. Ship **Python-first**; gate Java taint findings behind the existing resolution-coverage metric. Job 2 (later codegen) must refuse to ground on AMBIGUOUS edges — "verifiable grounding" verified against guesses is not grounding.

---

## 8. Incremental re-run (the one place "just check changed code" is unsafe)

- `body_hash(self)` changed → re-run Layer 3 facts + Agent 1 for that function. Correct for self-contained findings.
- **The hole:** Agent 2 findings depend on *neighbor* context. If neighbor `e` changes, `a`'s own `body_hash` is unchanged — but `a`'s analysis *saw* `e` and may now be stale. Self-hash alone misses it.
- **Fix:** cache each finding keyed by `body_hash(owning_fn) + body_hashes(neighbors in context)`. Change `e` → any finding whose bundle included `e` re-runs; change nothing → skip. Without this, re-runs silently keep stale cross-function findings.
- Cascade stays bounded to hop 1 **only while identity generation is name-based** (it is today). Re-verify that bound if identity ever encodes neighbor internals.

---

## 9. Build order

1. **Layer 3** — `taint.py` + sink/sanitizer tags. *Start here.*
2. **Stage 1 taint pass** (injection leg) — reuses existing traversal.
3. **Seeded eval branch** — before tuning a single prompt.
4. **Stage 2 / Agent 1** (correctness + targeted prompt lines).
5. **Stage 2 / Agent 2** (dependency-set, escalation, god-node guard).
6. **Stage 1 remaining passes** — authz → stored-taint → concurrency pre-filter → perf.
7. **Stage 3** — blast + qualify + severity formula.
8. **Head-to-head vs. Copilot CLI** on the seeded branch.

> **Amendment note (A2):** insert the architecture/layering pass into step 6 (Stage 1 remaining
> passes), alongside authz/stored-taint/concurrency/perf.

---

## 10. What separates this from "LLM with a graph stapled on"

- **Deterministic-first.** Anything the graph can prove (taint, reachability, smells) never touches an LLM. Token win and accuracy win from the same source.
- **Targeting over ambient.** Stage 1 flags tell Stage 2 *where to look hard*. The model is never handed a file and asked "anything wrong?" — it's asked specific questions the graph decided were worth asking.
- **Attribution is structural.** Owning-fqn IDs make dedup and incremental re-run lookups, not LLM work.
- **Composition beats context.** Multi-hop injections are *compositions* across functions — exactly what a grep agent can't hold in one context window and what your graph computes exactly.

*The diff was primitive-pr's focusing mechanism — it made every question narrow and every claim checkable. Whole-codebase has no diff, so you earn focus from the targeted passes and attribution from owning-fqn IDs. You're not deleting the diff; you're replacing what it did.*

---

## 11. Improvements — what to add *after* detection works
The detection design is close to as-good-as-it-gets for static+LLM. The gap is everything that happens *after* a bug is found. A tool that detects well and manages findings badly still dies by run 3. These are ranked by whether they change outcomes, not by how clever they are.

### 11.1 Split "how bad" from "how sure" — do this first, it's cheap and load-bearing
Two different questions, currently mashed into one number:

- **Severity** = how bad if it's real.
- **Confidence** = how likely it's real.

Like a smoke alarm: "there might be a fire" (bad) is a separate axis from "did it smell real smoke or just burnt toast" (sure).

- Today a **proven** taint path (the graph traced it) and a **guessed** race (a model had a hunch) both print as "CRITICAL." The user can't tell the sure thing from the guess, hits one false alarm, and stops trusting the whole list.
- **Fix:** every finding carries `(severity, confidence, source)` where `source ∈ {graph_proven, llm_judged, llm_flagged_candidate}`. **Sort by severity, filter by confidence.** Stage-1 deterministic findings sit up top with a "not an opinion" badge.

### 11.2 Suppression / baseline — the single most likely reason the tool dies

- Static analyzers live or die on false-positive fatigue. Run 1 → 200 findings, user dismisses 80 as noise/won't-fix. No memory → run 2 shows the same 80 → user quits. (The friend who reminds you to tie your shoes *every day forever* gets ignored completely.)
- The plan has **dedup** (same bug, same run) but no **baseline** (dismissed findings persist across runs).
- **Fix:** a suppression store keyed by the existing finding ID `hash(owning_fqn + category + subcategory + line_start)`. Dismissed IDs are filtered from future reports.
- **Free bonus:** the ID is body-hash-aware — if the code *changes*, the suppression lapses and the finding legitimately resurfaces. You already built the primitive; just point it here.
- **Higher value than adding another vuln class.** 90%-recall-buried-in-noise loses to 70%-recall-clean.

### 11.3 Name the taint precision level on purpose — it's your single point of failure

- "Tree-sitter finds the taint" hand-waves over the hardest part. Python taint is genuinely hard: container-element taint (taint a list → are the elements tainted?), dict/attr access, `*args`/`**kwargs`, closures capturing tainted vars, comprehensions. Field/container-sensitive taint is a research problem, not a week of tree-sitter.
- **Right v1 call: coarse, over-tainting.** Track at *argument granularity*; any container touched by tainted data is tainted whole; don't track individual fields. This **over**-reports — accept it.
- **Honest consequence — a real reframe of §5:** with coarse taint, the injection leg is **high-recall / medium-precision**, *not* proof-grade. Precision comes back via the Stage-3 qualify step (LLM prunes the false alarms). Still beats Copilot on recall. **Do not sell coarse taint as "proof."**
- **Falsifiable gate:** §2's exit test checks recall (find 4 of 5). Add a *precision* check — seed 5 clean paths that superficially look tainted; confirm the coarse extractor's FP rate is something Stage 3 can absorb.

### 11.4 Your eval grades its own homework — distrust the number

- Seeded-recall tests whether you can find bugs *you designed to be findable*. It says nothing about the classes you never thought to seed. (Write the test *and* the answers → of course you score high. The CSR GraphRAG "93%" had the same latent bias: 93% on *your* 15 questions.)
- **Two cheap corrections:**
  - Run against a repo with **known historical bugs / CVEs you didn't author** → real-world recall, not self-graded.
  - Run against a **known-clean module** with no seeded targets and measure pure **false-positive rate**. This is the number that predicts adoption, and seeded evals never surface it.

### 11.5 Cluster findings in the report — dedup, one level up

- Dedup kills the *same bug reported twice*. It doesn't touch *the same antipattern in 15 files* — 15 real, separate findings that are **one fix**. (A teacher marking the same spelling mistake on 15 papers vs. one "the class got this word wrong.")
- **Fix:** reuse Layer 2 embeddings to cluster findings by pattern similarity → "string-built-query antipattern in 15 places" collapses to one actionable item with 15 locations.
- Actionability, not detection. A 200-item flat list and a 30-cluster grouped list have very different odds of getting acted on.

### Smaller / lower-priority

- **Agent 2 vs. Stage-3 qualify overlap.** Both read callers to judge impact. For Agent-2-sourced findings that's the same question twice → skip qualify when the finding already carries Agent-2 caller judgment. Saves calls, no accuracy loss. (For Agent-1-sourced findings, qualify still does real new work — keep it there.)
- **Priority queue over flat queue in Stage 2.** Correctness needs no ordering, but *early value* does — analyze endpoint-reachable / high-centrality nodes first so criticals surface before the budget's spent. Only matters under a cost cap.

### Deliberately NOT doing (discrimination, not a checklist)

- **Cross-language taint** (Python→Java over HTTP). Real gap, but a boundary to *name in §5*, not build — enormous cost, sail-packages probably doesn't need it, speculative payoff.
- **Feedback loop** (dismissed-vs-fixed → tune future runs). Already known-unbuilt (§16.8 of the Brain doc). Useful eventually, but v2, and depends on 11.2 existing first anyway.

### Priority

- **11.1, 11.2, 11.4 — before any new bug class.** They're the difference between "impressive demo" and "thing people keep running."
- **11.3 — decide when you build taint.** A decide-now, don't-discover-later choice.
- **11.5 — wait.** At 163 functions the flat list is probably fine. *Uncertain this earns its complexity at your scale* — build it when the list *feels* annoying, not before.

**Confidence on my own claims:** high on 11.1 / 11.2 / 11.3 (well-established failure modes of real analyzers). Lower on 11.5 paying off at sail-packages size. That's the one to treat as "maybe, later."
