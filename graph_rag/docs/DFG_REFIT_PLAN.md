# Graph DFG + Heuristic Cleanup + Analyzer Refit — Detailed Implementation Spec

**Audience:** implementer (Sonnet) executes phases in order; validator (Opus) checks each phase against its Acceptance Criteria before the next phase starts.
**Project root:** `/Users/abhinav/Desktop/Projects/pr-review/pr-review/graph_rag/` — the Python package is `graph_rag/graph_rag/` (note the doubled dir). All paths below are relative to project root. Read `CLAUDE.md` at project root before starting.

---

## 0. Context and locked decisions

The system: `graph_core/` builds a structural code graph (tree-sitter → resolver → SCIP → derivations → Neo4j); `analyzer/` runs a 2-agent review over it (Agent A: correctness/impact per chunk; Agent B: taint + architecture in `analyzer/taint.py`); `rag/` is a separate tool — **do not touch `rag/` or `webapp/`**.

Problems being fixed:
1. The graph lacks data-flow information (which param reaches which callee arg / return / field).
2. Fuzzy substring heuristics (`"auth" in name`, event calls named `send`/`on`, package-substring roles) create untrustworthy edges/tags.
3. `analyzer/taint.py` duplicates AST walking: its `analyze_function` (line 257) / `analyze_function_java` (line 498) already compute per-function flow summaries, but privately, security-only, stored as opaque `taint_json`.
4. Sink classification runs inside the body_hash-cached extraction, so editing `SINK_PATTERNS` (taint.py:94) has no effect on unchanged functions — a live correctness bug.

**Owner's locked decisions (do not revisit):**
- **DFG = function-summary level.** `dfg_json` property on Function nodes + flow properties on existing edges. NO statement/variable nodes, NO Parameter nodes, NO new edge types.
- **Indexing stays 100% deterministic.** No LLM calls anywhere in `graph_core/`. LLMs remain analysis-stage only.
- **Fuzzy heuristics are kept but downgraded** to `confidence=AMBIGUOUS` + `strategy="fuzzy_name"` so consumers can filter — not deleted.
- **Analyzer keeps the 2-agent shape.** Agent B re-founds on the graph DFG; plus targeted perf/dedup fixes.
- **Python + Java both**, in every phase.

**Hard constraints:**
- `store.py:write_edges` does `MERGE (a)-[r:TYPE]->(b)` — at most ONE edge per (src, dst, type). All per-call-site flow facts must aggregate into parallel-array properties on a single PASSES edge.
- `models.py:_clean` (line 25) drops `""`, `0`, `None`, `False`, and empty containers from props. Never design a property whose meaningful value is scalar `0`/`False`. Lists containing `0` (e.g. `[0]`) are safe.
- Every edge carries confidence/origin/extractor/evidence (`models.py:Edge`).
- `schema.py` allowlists are the Cypher-injection guard; we add NO new labels or edge types.
- `./.venv/bin/python validate_fixtures.py` must exit 0 at the end of every phase. It mirrors the pipeline step-for-step in `build_graph()` — keep that mirror when the pipeline changes.
- Each phase = one commit, independently shippable.

---

## Phase 1 — Heuristic downgrade + tag hygiene

### 1.1 Thread a fuzziness marker through RawRef → resolver

`graph_core/models.py` — add one field to `RawRef` (after `arg_names`, line 183):
```python
strategy_hint: str = ""  # "fuzzy_name" when the ref came from a substring/loose heuristic
```

`graph_core/resolver.py` — find where event/policy/auth RawRefs become edges (the code paths using `_normalize_event_name` line ~653 and `_normalize_policy_name` line ~660, and wherever `REQUIRES_AUTH`/`ENFORCES_POLICY`/`EMITS_EVENT`/`CONSUMES_EVENT` refs are materialized). For any ref with `strategy_hint == "fuzzy_name"`: set the resulting Edge `confidence=Confidence.AMBIGUOUS.value` and `strategy="fuzzy_name"`. All other refs behave exactly as today.

`graph_core/extractors/python.py` and `java.py`: the local `ref(...)` closure builds RawRefs — add a passthrough `strategy_hint=""` kwarg.

### 1.2 Python extractor — `graph_core/extractors/python.py`

**`_auth_specs` (line 880):** change return type to `list[tuple[str, str, bool]]` (edge_type, target, fuzzy):
```python
def _auth_specs(dname, src, deco_node):
    lower = (dname or "").lower()
    out = []
    if lower in _AUTH_REQUIRE_DECORATORS:
        out.append(("REQUIRES_AUTH", "AUTH_REQUIRED", False))
    elif "auth" in lower:
        out.append(("REQUIRES_AUTH", "AUTH_REQUIRED", True))          # fuzzy
    if lower in _POLICY_DECORATOR_TOKENS:
        out.append(("ENFORCES_POLICY", <target as today>, False))
    elif any(t in lower for t in ("role", "permission", "policy", "scope")):
        out.append(("ENFORCES_POLICY", <target as today>, True))      # fuzzy
    return out
```
Call site (line 261-262): `ref(et[0], mid, et[1], "policy", dnode, strategy_hint="fuzzy_name" if et[2] else "")`. There is a second call site for class decorators — grep `_auth_specs(` and update all.

**Extend exact lists (line 860-866):** `_AUTH_REQUIRE_DECORATORS` += `jwt_required`, `token_required`; `_POLICY_DECORATOR_TOKENS` += `permission_required`, `roles_required`, `roles_accepted`.

**Event calls (lines 871-926):** split the sets:
```python
_EVENT_EMIT_CALLS_STRONG = {"emit", "publish", "produce", "send_event", "publish_event", "dispatch_event"}
_EVENT_EMIT_CALLS_GENERIC = {"send", "dispatch"}
_EVENT_CONSUME_CALLS_STRONG = {"subscribe", "consume", "add_listener"}
_EVENT_CONSUME_CALLS_GENERIC = {"listen", "on"}
_EVENT_RECEIVER_HINTS = {"bus", "broker", "producer", "consumer", "emitter", "events",
                         "event_bus", "kafka", "queue", "topic", "pubsub", "publisher", "channel"}
```
`_outbound_event`/`_inbound_event` return `(topic: str, fuzzy: bool)` instead of `str`:
- name in STRONG set → `(topic, False)`.
- name in GENERIC set: compute receiver tail via the existing `_receiver(src, fn)` helper (used at line 293). Receiver tail in `_EVENT_RECEIVER_HINTS` → `(topic, False)`; otherwise → `(topic, True)` (kept, fuzzy).
- neither → `("", False)`.
Update call sites (lines 312-317) to pass `strategy_hint="fuzzy_name"` when fuzzy.

**`_event_consumer_topic` (line 899):** the `"listener" not in lower and "consumer" not in lower` substring path likewise returns a fuzzy flag; exact `_EVENT_CONSUMER_DECORATORS` matches stay non-fuzzy. Update the call site at line 263-265.

**`_outbound_call` (line 836):** after obtaining `url` (and `verb` for the `request` variant), add a URL-shape guard:
```python
def _looks_like_url(s: str) -> bool:
    return s.startswith(("/", "http://", "https://"))
```
If the guard fails, return `None` (no CALLS_API, no Endpoint node). Rationale: `session.get("cache_key")` would otherwise fabricate an Endpoint node named `cache_key` — a fabricated entity, not a low-confidence fact, so this one is dropped rather than downgraded. `HTTP_CLIENT_RECEIVERS` in `apispec.py` stays unchanged.

### 1.3 Java extractor — `graph_core/extractors/java.py`

Mirror 1.2 exactly:
- `_java_auth_policy_specs` (line ~663-667): exact-set matches non-fuzzy; the `"auth" in low` and `any(t in low for t in ("role","permission","policy","scope","authorize","secured"))` paths return fuzzy=True → `strategy_hint="fuzzy_name"`. Extend `_AUTH_REQUIRE_ANNOTATIONS` += `RequiresAuthentication`; `_POLICY_ANNOTATIONS` += `RequiresRoles`, `RequiresPermissions`.
- `_EVENT_EMIT_METHODS`/`_EVENT_CONSUME_METHODS` (lines 58-63): same STRONG/GENERIC split (`publishEvent` is STRONG; bare `send`/`on`/`listen`/`dispatch` GENERIC) with the same receiver-hint gate — extract receiver from the `method_invocation`'s `object` field. Annotation-driven consumers (`_EVENT_CONSUMER_ANNOTATIONS`) unchanged — already exact.

### 1.4 Roles — `graph_core/pipeline.py`

- `_CLASS_ROLE_RULES` (~line 258-285): for rules with match-kind `"pkg"`, change the test from substring (`key in pkg`) to exact dotted-segment match: `key in pkg.lower().split(".")`. Rule keys become bare segments (`controller`, `service`, `repo`, `repository`, `config`). Fixes `.repo` matching `com.acme.reports`.
- `_classify_roles` (~line 324-371): **delete the `helper` fallback assignment** (`assign(n, "helper", "no_owning_class", "LOW")` at ~line 371). Leave `component_role` empty — downstream already coalesces empty to `"unknown"` (`analyzer/taint.py:1385`).
- Update the two analyzer prompts that mention "helper": `_STAGE1_SYSTEM` (taint.py ~1420) and `_STAGE2_SYSTEM` (taint.py ~1533) — replace helper-role language with "unknown/unlabeled role" language.
- Keep suffix rules (MEDIUM) and segment-matched package rules (LOW) as-is.

### 1.5 Name normalization — `graph_core/resolver.py`

- `_normalize_event_name` (~line 653): canonicalize to dotted-lower: insert `.` at camelCase boundaries (`re.sub(r"(?<=[a-z0-9])(?=[A-Z])", ".", s)`), lowercase, replace `_`, `-`, `::`, `/`, whitespace runs with `.`, collapse repeated `.`. `OrderPlaced` → `order.placed`; `order_placed` → `order.placed`. The Event node's `name`/`fqn` use the normalized key (one node per logical event); `display_name` = first-seen raw string.
- `_normalize_policy_name` (~line 660): unify separators/case the same way but do NOT strip `ROLE_` prefixes (Spring treats `ROLE_ADMIN` ≠ `ADMIN`).

### 1.6 Fixtures + gate

- New fixture `fixtures/live_test/noise.py`:
  ```python
  import requests
  session = requests.Session()
  def cache_lookup(key):
      return session.get("cache_key")        # must NOT create a CALLS_API/Endpoint
  def notify(smtp, button):
      smtp.send("hello")                      # EMITS_EVENT only as AMBIGUOUS/fuzzy_name
      button.on("click")                      # CONSUMES_EVENT only as AMBIGUOUS/fuzzy_name
  def helpful_thing():                        # must NOT get component_role "helper"
      return 1
  ```
- New fixture emitting `"order_placed"` (string topic) alongside the existing `OrderPlaced` emitter so normalization can be asserted.
- Java fixture: a class in package `com.acme.reports` that must NOT be tagged `repository`.
- `validate_fixtures.py` new assertions:
  - No Endpoint node with route containing `cache_key`.
  - Every `EMITS_EVENT`/`CONSUMES_EVENT`/`REQUIRES_AUTH`/`ENFORCES_POLICY` edge with `strategy == "fuzzy_name"` has `confidence == "AMBIGUOUS"`; edges to events `hello`/`click` are fuzzy-only.
  - No node has `component_role == "helper"`; the `com.acme.reports` class has no `repository` role.
  - Exactly ONE Event node for {`OrderPlaced`, `order_placed`} (same node id), with `display_name` set.
  - All existing count assertions still pass — if `ENFORCES_POLICY >= 2` or `REQUIRES_AUTH >= 1` relied on a substring path, either the assertion accepts AMBIGUOUS edges or the fixture decorator moves into the exact list. Check which decorators `fixtures/` actually use before deciding.

### Phase 1 Acceptance Criteria (validator)
1. `validate_fixtures.py` exits 0, including all new assertions above.
2. `grep -n 'in lower' graph_rag/graph_core/extractors/python.py` — every remaining substring match feeds a fuzzy=True path, none emits a non-AMBIGUOUS edge.
3. No occurrence of `"helper"` as a role in `graph_core/` (grep `'helper'`).
4. `measure_coverage.py fixtures/live_test --repo t` runs clean; CALLS coverage unchanged from pre-phase baseline (record baseline first).
5. Diff review: no edge type added/removed from `schema.py`; `RawRef` gained only `strategy_hint`.

---

## Phase 2 — DFG: new `graph_core/dataflow.py`

### 2.1 Data model

**New file `graph_core/dataflow.py`.** Core types:

```python
@dataclass
class ArgFlow:
    callee: str              # dotted-tail call name, e.g. "execute"
    recv: str                # receiver tail, e.g. "conn" ("" for bare calls)
    callee_id: str = ""      # graph node id once bound (2.3); "" = unresolved/external
    arg_position: int | None = None   # positional index; None for keyword/splat
    arg_keyword: str | None = None    # keyword name; "*"/"**" for dynamic splats
    from_params: list[int] = ...      # caller param indices flowing into this arg
    from_fields: list[str] = ...      # field names (e.g. "User.name") flowing into this arg
    is_literal: bool = False          # True when the arg is a literal/constant expr
    line: int = 0

@dataclass
class DfgSummary:
    passes: list[ArgFlow]             # EVERY argument of every call in scope (not only tainted ones)
    returns_from_params: list[int]
    returns_from_fields: list[str]
    field_writes: list[dict]          # {"field": "Cls.attr", "from_params": [..], "from_fields": [..], "line": n}

    def to_json(self) -> str: ...     # json.dumps(sort_keys=True), same style as FunctionTaint.to_json
```

### 2.2 `summarize_function(src, func_node, param_names, lang, owner_class="") -> DfgSummary`

**Port `analyzer/taint.py:analyze_function` (lines 257-426) and `analyze_function_java` (line 498+) into `dataflow.py`, generalized.** Copy the algorithm faithfully — it is battle-tested — with these deltas:

1. **No sink classification.** Delete every `classify_sink` call and the `sinks` list; a sink is just another `ArgFlow` now. Do NOT import anything from `analyzer/`.
2. **Record every argument**, not only tainted ones: emit an `ArgFlow` per positional/keyword/splat-literal arg. `from_params`/`from_fields` may be empty; set `is_literal=True` when the arg node is a string/number/true/false/none literal (tree-sitter types: `string`, `integer`, `float`, `true`, `false`, `none`; Java: `string_literal`, `decimal_integer_literal`, etc.).
3. **Field origins (Python):** maintain a second taint map `field_taint: dict[str, set[str]]` alongside `param_taint` (the existing `tainted` dict renamed). Seed nothing; during the walk, when an RHS/argument expression contains an `attribute` node whose object is `self` (Java: `field_access` with object `this`), add origin `f"{owner_class}.{attr}"` (or bare attr if owner unknown). Assignments propagate both maps identically.
4. **Field writes:** when the assignment LHS is `self.<attr>` (Python `assignment` whose `left` is an `attribute` with object `self`; Java `assignment_expression` whose left is `field_access` on `this`), emit a `field_writes` entry with the RHS's `from_params`/`from_fields`.
5. **Returns:** existing `returns_from_params` logic verbatim; add `returns_from_fields` from `field_taint` the same way.
6. Move `_TAINT_INERT_BUILTINS`, `_identifiers`, `_own_scope`, `_callee_parts`, `_call_lhs_assign_target`, `_splat_literal_entries` (and their `_java_*` twins, plus `_own_scope_java`, `_java_identifiers`, `_java_callee_parts`, `_java_call_lhs_assign_target`) into `dataflow.py` (copy now; Phase 3 deletes the originals). Same-function conservative return-flow propagation (taint.py:406-415) comes along verbatim.
7. Filter noise: skip emitting `ArgFlow` rows for callees in `_TAINT_INERT_BUILTINS` **unless** some `from_params`/`from_fields` is non-empty (keeps dfg_json compact; preserves current taint semantics).

### 2.3 `run_dataflow(files, all_nodes, all_edges, repo) -> DataflowResult`

```python
@dataclass
class DataflowResult:
    node_props: dict[str, dict]   # function node id -> {"dfg_json":..., "dfg_returns_from_params":..., "dfg_hash":...}
    passes_edges: list[Edge]      # aggregated PASSES edges (replaces extractor PASSES)
    writes_from_params: dict[tuple[str, str], list[int]]  # (fn_id, field_id) -> param idxs
    stats: dict                   # {"functions": n, "bound": n, "unbound": n}
```

Steps:
1. Re-parse each python/java file with `languages.get_parser` (same as extractors; passing trees through is Phase 5). Locate each Function node's AST subtree by `start_line` (Function nodes carry `file`/`start_line`/`param_names` — build an index from `all_nodes`).
2. `summarize_function` per function; `dfg_hash = node.body_hash`.
3. **Bind** each `ArgFlow` to a callee node: build `calls_by_src: dict[src_id, list[Edge]]` from resolved CALLS edges in `all_edges` (post-SCIP, so bindings inherit SCIP precision). Candidates = CALLS edges from this caller whose dst node `name` == `ArgFlow.callee`. Unique candidate → set `callee_id`; zero or multiple → leave `""` (composition falls back by name).
4. **Keyword → position:** when `callee_id` is bound and `arg_keyword` is set, map it to the callee's `param_names` index (port `_resolve_arg_position`, taint.py:823, including the `self`-offset handling for methods). Store the mapped index in the PASSES edge arrays; `dfg_json` keeps the original keyword too.
5. **Aggregate PASSES edges:** group bound ArgFlows by `(caller_id, callee_id)`. One `Edge(type="PASSES", origin=DERIVED, extractor="dataflow")` per group with properties (parallel arrays, index-aligned per recorded arg):
   - `flow_from_param: list[int]` — caller param index; `-1` when the arg has no param origin (literal/local/field).
   - `flow_to_param: list[int]` — callee param index; `-1` when unmappable (dynamic splat).
   - `flow_lines: list[int]` — call-site lines.
   - `const_args: list[int]` — callee param positions receiving only literal args across all call sites.
   - `arg_names` — keep populating for display (reuse the identifier-name logic from the extractor's `_pass_arg_names`).
   - `confidence` — copy from the CALLS edge that bound it; `evidence_file/line` = first call site.
6. Function node props: `dfg_json` (full summary incl. unbound ArgFlows), `dfg_returns_from_params`, `dfg_hash`.
7. WRITES enrichment: for each `field_writes` entry, locate the existing WRITES edge (src=fn, dst=field node matched by name within owner class) and record `from_params` into `writes_from_params`.

### 2.4 Wiring

- **`graph_core/models.py`:** `Edge` gains `flow_from_param`, `flow_to_param`, `flow_lines`, `const_args` (all `list[int]`, default empty) — add to `Edge.props()`. `Node` gains `dfg_json: str = ""`, `dfg_returns_from_params: list[int]`, `dfg_hash: str = ""` — add to `Node.props()`.
- **`graph_core/pipeline.py:index_repo`:** insert after the scip-java block (line ~92), before `_derive_overrides` (line 96):
  ```python
  dfg = run_dataflow(files, all_nodes, all_edges, repo)
  all_edges = [e for e in all_edges if e.type != "PASSES"]   # extractor PASSES superseded
  all_edges.extend(dfg.passes_edges)
  # apply dfg.node_props onto all_nodes; apply writes_from_params onto WRITES edges
  ```
  Add `dfg: dict` to `IndexResult` (the `stats`), print it in the CLI index summary.
- **Extractors:** delete the PASSES `ref(...)` emission in `python.py` (lines 297-308) and its Java analogue (grep `"PASSES"` in `extractors/`). Delete `_pass_arg_names` from extractors only if unused after the move (dataflow.py takes over the name extraction).
- **`graph_core/schema.py`:** update the `PASSES` comment (line 53) to: `# argument/data-flow summary: flow_from_param/flow_to_param parallel arrays (function-summary DFG)`. No allowlist changes.
- **`CLAUDE.md`:** document the new pipeline step 5.5 (Dataflow) and the PASSES payload.

### 2.5 Fixtures + gate

- Add to an existing Python fixture: a function that returns its param (`def echo(x): return x`), a function writing a param to `self.field`, and a 2-hop chain `handler(user_input) → service.process(user_input) → repo.save(user_input)`. Java: mirror with `this.field = param`.
- `validate_fixtures.py:build_graph` gains the dataflow step in the same position as the pipeline.
- New assertions:
  - ≥ 4 PASSES edges with non-empty `flow_from_param` AND `flow_to_param`.
  - The handler→service PASSES edge has `0 in flow_from_param` and `flow_to_param` maps to the callee's param index.
  - ≥ 1 Function node with `dfg_returns_from_params == [0]` (the `echo` fixture).
  - ≥ 1 WRITES edge with non-empty `from_params`; Java equivalents for each.
  - Old `PASSES >= 2` assertion still passes (now produced by dataflow, not extractors).

### Phase 2 Acceptance Criteria (validator)
1. `validate_fixtures.py` exits 0 with all 2.5 assertions.
2. `grep -rn '"PASSES"' graph_rag/graph_core/extractors/` → no emission sites remain.
3. `grep -n 'import' graph_rag/graph_core/dataflow.py` → no `analyzer` imports; no `llm` imports (determinism).
4. Index a fixture repo into Neo4j; spot-check in browser: `MATCH (a)-[p:PASSES]->(b) WHERE 0 IN p.flow_from_param RETURN a.fqn, b.fqn, p.flow_to_param` returns the fixture chain.
5. `analyzer/taint.py` untouched this phase (`git diff --stat` shows no analyzer changes except none).
6. Indexing time on `fixtures/` within ~2x of baseline.

---

## Phase 3 — Agent B re-founded on the graph DFG

### 3.1 New `analyzer/sinks.py`

- `DEFAULT_SINKS: list[dict]` — restructure `SINK_PATTERNS` (taint.py:94-128) + `_SINK_RECEIVER_HINTS` (taint.py:132-140) into records: `{"vuln_class": str, "names": [str], "receivers": [str] | None, "langs": ["python","java"]}` (`receivers: None` = any; preserve the current hint semantics exactly, including the `""` empty-receiver entry for path_traversal).
- `load_sinks(repo_root: str) -> list[dict]` — start from `DEFAULT_SINKS`; if `$GRAPH_RAG_SINKS` points to a JSON file, merge; if `<repo_root>/.graphrag-sinks.json` exists, merge on top. Merge semantics per record: same `vuln_class` extends `names`/`receivers`; `{"vuln_class": X, "disabled": true}` removes it.
- Move `classify_sink` here (same signature `(recv, name) -> str | None`, now closing over loaded sinks — make it a small class or partial).
- `_SANITIZER_NAME_HINTS` regex moves here too.

### 3.2 Deletions from `analyzer/taint.py`

Delete (now living in `graph_core/dataflow.py` or `analyzer/sinks.py`): `SINK_PATTERNS`, `_SINK_RECEIVER_HINTS`, `classify_sink`, `_TAINT_INERT_BUILTINS`, `_callee_parts`, `_identifiers`, `_own_scope`, `_call_lhs_assign_target`, `_splat_literal_entries`, `FunctionTaint`, `analyze_function`, all `_java_*` extraction twins + `analyze_function_java`, and `run_taint_pass` (line 587-~660). Roughly lines 77-660 minus the docstring. Update the module docstring: pass 1 is now "read `dfg_json` written at index time".

### 3.3 Composition reads the graph

- Wherever composition bulk-loads `taint_json` (grep `taint_json` in `analyzer/`), load `dfg_json` instead. Adapter shape: a dfg `ArgFlow` with `classify_sink(recv, callee)` match = old "sink" fact (vuln_class from the match); non-matching ArgFlows with `callee_id` or resolvable name = old "passes" fact; ArgFlows with no in-repo resolution and callee not inert = the `unresolved_external_taint_flow` signal (preserve taint.py:1103-1130 behavior).
- **Sink classification happens HERE, at walk time** — this is the payoff: sink config changes take effect without reindexing.
- Callee resolution: prefer `ArgFlow.callee_id`; fall back to the existing `_resolve_callees` (taint.py:809) name matching only when `callee_id == ""`. `_resolve_arg_position` becomes a fallback too (bound flows already carry mapped positions via PASSES / dfg_json).
- `find_sanitizer_candidates` (taint.py:674): candidates from `dfg_json.passes[].callee` + name-hint regex from `sinks.py`. `_own_sink_params` classifies from ArgFlows on the fly.
- Sanitizer tagging (`tag_sanitizers`, LLM) unchanged otherwise — analysis-stage LLM use is allowed.

### 3.4 Migration + wiring

- `analyzer/agents.py:_function_spans` (line 91): stop reading `n.taint_sink_count`. Instead `run_agent_a_scan` computes a `dict[fqn, bool]` of sink-reaching functions in one pass (load all `dfg_json` for the repo, classify against loaded sinks) and threads it to `run_agent_a_chunk`'s targeted-checks block.
- `cli.py:_cmd_analyze`: remove the `run_taint_pass` call. Add a guard: `MATCH (n:Function {repo:$repo}) WHERE n.dfg_json IS NOT NULL RETURN count(n)` — if 0 while functions exist, exit with error `"this repo was indexed before DFG support — re-run: graph_rag.cli index <path> --repo <name>"`. One-time cleanup: `MATCH (n:Function {repo:$repo}) WHERE n.dfg_json IS NOT NULL REMOVE n.taint_json, n.taint_hash, n.taint_sink_count`.
- Keep `--no-llm` working: deterministic composition + cycle sweep only, as today.

### Phase 3 Acceptance Criteria (validator)
1. `validate_fixtures.py` exits 0 (it doesn't run the analyzer, so also:)
2. End-to-end: `index` a fixture repo → `analyze --no-llm` → the seeded taint chain (fixtures contain known source→sink paths; check `fixtures/seeded_eval/`) produces the same `graph_proven` findings as the pre-phase baseline (capture baseline JSON with `--json` before starting Phase 3; diff finding sets on `(owning_fqn, subcategory, line)`).
3. Sink-config live test: add `.graphrag-sinks.json` with a custom sink name used in a fixture, re-run `analyze` WITHOUT re-indexing → new finding appears.
4. `grep -n 'analyze_function\|FunctionTaint\|SINK_PATTERNS' graph_rag/analyzer/taint.py` → no hits.
5. `grep -rn 'taint_json' graph_rag/` → only the cleanup REMOVE statement remains.
6. taint.py shrinks by ≥ 400 lines.

---

## Phase 4 — Analyzer performance + unified dedup

### 4.1 Batch Agent A context queries (`analyzer/agents.py`)

Replace per-function `_callers` (line 121) + per-caller `_hop2_summary` (line 133) with two batched queries per chunk:
```cypher
MATCH (c:Function)-[:CALLS]->(m:Function) WHERE m.id IN $ids
WITH m, c LIMIT 4000
RETURN m.id AS target, collect({id:c.id, fqn:c.fqn, name:c.name, file:c.file,
        signature:c.signature, docstring:c.docstring, role:c.component_role,
        role_conf:c.role_confidence})[..40] AS callers
```
and one grouped hop-2 query over all collected caller ids returning `(caller_id, role, count)`. Preserve the existing `_FANOUT_GUARD=40` cap and output formatting exactly (prompt text must not change — findings baseline depends on it).

### 4.2 Concurrency

- First verify the LLM client (`graph_core/llm.py` or wherever `llm.extract` lives) is thread-safe (per-call HTTP session). If not, create one client per worker.
- `ThreadPoolExecutor(max_workers=6)` around: (a) Agent A chunk calls in `run_agent_a_scan` (keep per-chunk try/except and the skip log), (b) `qualify_taint_finding` calls in `run_taint_qualify`, (c) `_analyze_shape_instance` calls in `analyze_shape`. Collect results in deterministic order (sort by input order, not completion).

### 4.3 Source-block cache

Per-run cache `dict[function_id, str]` for the source+imports+shared-state blocks built by `_chain_source_blocks` (taint.py:1195), shared by qualify and stage-2. Build lazily; no invalidation needed within a run.

### 4.4 Unified dedup — new `analyzer/dedup.py`

- `FAMILY: dict[str, str]` mapping subcategory → family (e.g. `sql_injection`, `command_injection` map to themselves; correctness twins map to the security family; extend as drift is found).
- `dedupe_findings(findings) -> list[Finding]`: key = `(owning_fqn, FAMILY.get(subcategory, subcategory), line_bucket)` where `line_bucket = line // 5`. On collision keep by provenance priority `graph_proven > llm_judged`, then higher confidence, then first-seen.
- Absorbs and replaces: `findings.dedupe` + `collapse_cross_category_duplicates` (`graph_core/findings.py:162-207` — keep thin wrappers delegating to dedup.py or update the callers), the local `seen` in `analyze_shape` (taint.py:1607). `cli.py` calls it exactly once before scoring.

### Phase 4 Acceptance Criteria (validator)
1. `analyze` on a fixture repo produces the same finding set as the Phase 3 baseline (keys `(owning_fqn, subcategory)`; ordering may differ).
2. Neo4j query count for Agent A context drops from O(functions) to O(chunks) — verify by counting `store` calls with a debug counter or log.
3. Wall-clock `analyze` time improves ≥ 2x on a multi-file fixture with LLM enabled (or skip if no API key; then verify with `--no-llm` that batching changed no findings).
4. Exactly one dedup entry point: `grep -rn 'collapse_cross_category_duplicates\|dedupe(' graph_rag/` shows only dedup.py internals + one cli.py call site.

---

## Phase 5 (optional, separate approval)
- Deterministic pre-qualify in `scoring.py:qualify` using PASSES `const_args`/`flow_from_param` (constant-only callers pruned without an LLM call).
- `taint_sources: list[int]` on Function nodes — mark handler params bound to request data (FastAPI annotation types, Flask `request.*` reads) so composition seeds only attacker-reachable params.
- Pass extractor tree-sitter trees into `run_dataflow` to avoid the re-parse.

---

## Defaults chosen (owner may veto, otherwise proceed)
- Non-URL-shaped CALLS_API dropped entirely (fabricated Endpoint nodes), unlike other fuzzy heuristics which are kept as AMBIGUOUS.
- `helper` fallback role deleted (it's the "unnecessary tag"), not downgraded.
- Field-level flow (`from_fields`/`field_writes`) included in DFG v1.
- Sink config from both `.graphrag-sinks.json` (repo) and `$GRAPH_RAG_SINKS` (env), merged over code defaults.
- Event-name normalization always on; raw string preserved in `display_name`.
- Pre-DFG graphs: `analyze` hard-errors with a re-index message.

## Verification commands (every phase)
```bash
./.venv/bin/python validate_fixtures.py                                   # must exit 0
./.venv/bin/python measure_coverage.py fixtures/live_test --repo smoke    # coverage not regressed
# with Neo4j up (docker run ... neo4j:5, password testpassword):
./.venv/bin/python -m graph_rag.cli index fixtures/live_test --repo smoke
./.venv/bin/python -m graph_rag.cli analyze fixtures/live_test --repo smoke --no-llm
```
Baselines to capture BEFORE Phase 1: `measure_coverage` output; `analyze --no-llm --json` finding set on fixtures. Every phase's acceptance compares against these.
