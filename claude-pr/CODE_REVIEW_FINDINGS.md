# Code Review Findings — `claude-pr`

Full read-through of all 16 modules (~4,300 lines). Findings are ordered by priority:
**A. Bugs** (things that are wrong today) → **B. Performance** → **C. Dead code** →
**D. Repo hygiene** → **E. Smaller improvements**.

Each finding has the exact location, the evidence, why it matters, and a concrete fix.

---

## A. Bugs

### A1. TLS certificate verification is disabled on all GitHub calls 🔴 (security)

**Location:** `pr_review/github_client.py:40`

```python
self.s = requests.Session()
self.s.verify = False        # ← every API call skips TLS cert verification
```

**Why it matters:** Every request to `api.github.com` carries the user's Personal
Access Token in the `Authorization` header. With `verify=False`, a
man-in-the-middle (hostile Wi-Fi, proxy) can present any certificate and read
the token, the repo source tarballs, and the diffs. It also makes `urllib3`
print `InsecureRequestWarning` spam on every call.

**Fix:** Delete the line. If it exists to survive a corporate TLS-intercepting
proxy, make it an explicit opt-out instead of the default:

```python
self.s = requests.Session()
# Only for corporate MITM proxies; never disable silently.
if os.environ.get("PR_REVIEW_INSECURE_TLS") == "1":
    self.s.verify = False
```

---

### A2. `Neo4jStore.callers()` sends invalid Cypher 🔴

**Location:** `pr_review/neo4j_store.py:134-138`

```python
rows = self._run(
    "MATCH (caller:Node)-[:CALLS*1..{d}]->(n:Node {{id: $id}}) "
    "RETURN DISTINCT caller.id AS id".replace("{d}", str(depth)),
    {"id": node_id},
)
```

**Why it matters:** This string is a *plain* string, not an f-string, so the
double braces `{{id: $id}}` are **not** collapsed to `{id: $id}` — they are sent
to Neo4j literally as `{{id: $id}}`, which is a Cypher syntax error. Only `{d}`
gets patched by `.replace()`. The query fails on every call, so
`callers()` always raises (or returns nothing if the error is swallowed
upstream). The double-brace style was copy-pasted from f-string/`.format()`
conventions without the `f` prefix.

**Fix:** Use a real f-string for the depth (which can't be parameterized in
Cypher) and single braces for the map; validate depth to keep it injection-safe:

```python
def callers(self, node_id: str, depth: int = 2) -> List[str]:
    if not self._available:
        return []
    depth = max(1, int(depth))   # depth can't be a query parameter in Cypher
    rows = self._run(
        f"MATCH (caller:Node)-[:CALLS*1..{depth}]->(n:Node {{id: $id}}) "
        "RETURN DISTINCT caller.id AS id",
        {"id": node_id},
    )
    return [r["id"] for r in rows]
```

---

### A3. Streamlit graph cache goes stale / never builds embeddings 🟠

**Location:** `streamlit_app.py:35` (`graph_cache`), `:232` (lookup), `:262` (store)

```python
ss.setdefault("graph_cache", {})     # sha -> (CodeGraph, EmbeddingIndex|None)
...
if head_ref in cache:
    cg, embed_idx = cache[head_ref]
```

Two distinct problems:

**(a) Branch-compare mode caches by branch *name*, not SHA.**
In PR mode `head_ref = pr.head_sha` (good). In compare mode
(`streamlit_app.py:209`) `head_ref = head_br` — the branch **name**. If the user
pushes new commits to that branch and re-runs the review, the cache hit at
`:232` returns the *old* snapshot's graph, and findings get pinned to line
numbers that no longer match the diff.

**Fix:** Resolve the branch to its current head SHA before caching. The
branches API already returns it — extend `list_branches` (or add a method):

```python
# github_client.py
def get_branch_sha(self, full_name: str, branch: str) -> str:
    data = self._get(f"{API}/repos/{full_name}/branches/{branch}").json()
    return (data.get("commit") or {}).get("sha", branch)
```
```python
# streamlit_app.py (compare mode)
head_repo, head_ref = repo, ss.gh.get_branch_sha(repo, head_br)
```
Also key the cache on `(head_repo, head_ref)` — two repos can share a SHA-less
branch name.

**(b) Embeddings are never built if the first run cached `None`.**
The tuple `(cg, embed_idx)` is stored once. If the first review ran at
Quick/Standard (no embeddings), `embed_idx is None` is cached; switching to
Deep later hits the cache and skips the `want_embeddings` branch entirely — the
Deep review silently runs without its semantic index.

**Fix:** Build lazily on cache hit:

```python
if cache_key in cache:
    cg, embed_idx = cache[cache_key]
    if want_embeddings and embed_idx is None and not dossier_only:
        with st.spinner("Building embedding index..."):
            embed_idx = build_index(cg)
        cache[cache_key] = (cg, embed_idx)
```

---

### A4. Agent tool loop duplicates findings and loses the final answer 🟠

**Location:** `pr_review/agents.py:245-280` (`BaseAgent.run`)

```python
for _round in range(MAX_TOOL_ROUNDS + 1):
    resp = nova.converse_with_tools(...)
    tool_uses = [b for b in resp if b.get("type") == "tool_use"]
    text_blocks = [b["text"] for b in resp if b.get("type") == "text"]

    for text in text_blocks:            # ← parses findings EVERY round
        found = _parse_findings(text)
        if found:
            findings.extend(found)

    if not tool_uses:
        break
    ...
```

Two problems:

**(a) Duplicate findings.** Findings are parsed and accumulated from *every*
round. Models frequently emit a partial findings array *and* a tool call in the
same turn, then re-emit the (overlapping) final array after the tool result.
Both get `extend()`ed → duplicates that only survive because `review.py` later
dedups on `(file, line, title[:40])` — and that dedup misses re-worded titles.

**(b) Lost final answer at the round limit.** If the model still requests tools
on the last iteration, the loop appends the tool results to `messages` and then
simply exits — the model never gets a chance to produce its findings, so the
agent silently returns `[]` (or only the partial early-round findings).

**Fix:** Keep only the *last* parsed findings, and after the round limit make
one final call without tools so the model must answer:

```python
def run(self, dossier, cg, embed_index, nova) -> List[Finding]:
    messages = [{"role": "user", "content": [{"text": self._user_prompt(dossier)}]}]
    for _round in range(MAX_TOOL_ROUNDS):
        resp = nova.converse_with_tools(self.system_prompt, messages, TOOLS)
        tool_uses = [b for b in resp if b.get("type") == "tool_use"]
        if not tool_uses:
            return self._findings_from(resp)        # terminal answer
        messages.append({"role": "assistant", "content": resp})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tu["id"],
             "content": execute_tool(tu["name"], tu.get("input", {}), cg, embed_index)}
            for tu in tool_uses
        ]})
    # round limit hit: force a final answer, no tools offered
    messages.append({"role": "user", "content": [
        {"text": "Tool budget exhausted. Output your final findings JSON now."}]})
    resp = nova.converse_with_tools(self.system_prompt, messages, tools=[])
    return self._findings_from(resp)

def _findings_from(self, resp) -> List[Finding]:
    out: List[Finding] = []
    for b in resp:
        if b.get("type") == "text":
            out.extend(_parse_findings(b["text"]))
    return out
```

(Note: `converse_with_tools` already omits `toolConfig` when `tools` is empty —
`llm.py:96-97` — so passing `tools=[]` works as-is.)

---

### A5. `parse_diff` mis-counts after `\ No newline at end of file` 🟠

**Location:** `pr_review/diff.py:60-69`

```python
elif line.startswith("+") and not line.startswith("+++"):
    cur.added_lines.add(new_lineno)
    new_lineno += 1
elif line.startswith("-") and not line.startswith("---"):
    pass
else:
    # context line: advances new-side line counter
    if new_lineno:
        new_lineno += 1
```

**Why it matters:** Unified diffs emit the marker line
`\ No newline at end of file` whenever a file's last line lacks a trailing
newline. That line starts with `\`, falls into the final `else`, and increments
`new_lineno` even though it is **not** a file line. Every added line after that
point in the same file is recorded one line too high — findings, chunk
boundaries, and `node_for_line` mapping all shift. This is common in real PRs
(any edit touching a file without a trailing newline).

**Fix:**

```python
elif line.startswith("\\"):
    pass    # "\ No newline at end of file" — not a file line
else:
    if new_lineno:
        new_lineno += 1
```

Related hardening (same function): the GitHub `.diff` format can include
`Binary files a/... and b/... differ` lines and `rename from/to` headers — they
currently hit the `else` branch too, but only after a `@@` hunk would
`new_lineno` be non-zero, so they're harmless today. The `\` fix is the one
that actually corrupts numbering.

---

### A6. Eval harness: judge index ambiguity corrupts FP counts 🟠

**Location:** `pr_review/eval.py:253-274`

```python
file_actuals = [d for d in actual_dicts if d["file"] == expected.file]
matched, idx, reason = _judge(expected, file_actuals, nova)
...
if idx is not None:
    matched_actual_indices.add(idx)
...
case_result.false_positives = (
    len(overall.all_findings) - len(matched_actual_indices)
)
```

**Why it matters:** The judge is shown `file_actuals` — a *filtered* list —
and the prompt asks for `"matched_index": int`. Each dict carries a global
`"index"` field, but nothing tells the model whether to return that field or
the *position in the filtered list it was shown*. When it returns the position
(the natural reading of "index of the matching actual finding"), the wrong
global finding is marked matched, and `false_positives` (computed against the
**global** list) is wrong in both directions. Two expected findings can also
"match" the same actual finding (the set dedups silently).

**Fix:** Make the contract explicit and validate:

```python
JUDGE_PROMPT = """...
"matched_index": int | null   // the "index" FIELD of the matching actual finding (not its position)
..."""

valid_indices = {d["index"] for d in file_actuals}
if idx is not None and idx not in valid_indices:
    idx = None          # judge returned a position / hallucinated index
```

Also consider passing *already-matched* indices to subsequent judge calls so
two expected findings can't claim the same actual one.

---

### A7. Truncated / throttled LLM responses are silently swallowed 🟠

**Location:** `pr_review/llm.py:46-57` (`complete`), `:99-100` (`converse_with_tools`), `:37-43` (retry config)

**Why it matters (truncation):** Neither call site checks
`resp["stopReason"]`. When the model hits `maxTokens` mid-array, the JSON is
cut off, `_extract_json` returns `None`, `_parse_findings` returns `[]`, and an
entire agent's findings vanish with no signal. With `max_tokens=4096` and the
verbose findings schema, this is reachable on busy files.

**Why it matters (throttling):** Bedrock throttles aggressively
(`ThrottlingException`). The client config is
`retries={"max_attempts": 2}` — standard mode, two attempts, no adaptive
backoff. With 6 agents × N chunks × up to 5 rounds fired back-to-back (and
worse once parallelized, see B1), reviews will die mid-run.

**Fix:**

```python
config=Config(connect_timeout=60, read_timeout=300,
              retries={"max_attempts": 6, "mode": "adaptive"}),
```

and surface truncation where the response is consumed:

```python
resp = self._client.converse(**kwargs)
if resp.get("stopReason") == "max_tokens":
    # caller can retry with a higher budget, or at minimum log it
    import logging
    logging.warning("Nova response truncated at max_tokens (%s)", self.model_id)
```

A cheap robustness add-on in `review._verify`: today, when the verifier's JSON
fails to parse it returns **all** candidates (`review.py:174-175`) — that's a
sane fallback, but combined with silent truncation it means "verifier ran" and
"verifier did nothing" are indistinguishable. Log/track that case.

---

## B. Performance

### B1. The whole review is serial — parallelize chunk × agent calls 🚀 (biggest win)

**Location:** `pr_review/review.py:232-237` and `:194-201` (`_review_chunk`)

```python
for chunk in chunks:
    cr = _review_chunk(chunk, cg, embed_index, nova, active_agents)   # serial
```
```python
for agent in active_agents:
    findings = agent.run(chunk.dossier, ...)                          # serial
```

**Why it matters:** A Deep review of a 5-file PR with 2 chunks/file runs
6 agents × 10 chunks = 60 agent runs, each potentially 1-5 LLM round-trips,
**strictly sequentially**. At ~5-15 s per call this is many minutes of wall
time, dominated entirely by network wait — perfect ThreadPool territory
(boto3 clients are thread-safe for `converse`).

**Fix sketch** (in `run_file_review`, replacing the chunk loop):

```python
from concurrent.futures import ThreadPoolExecutor

pairs = [(chunk, agent) for chunk in chunks for agent in active_agents]

def _one(pair):
    chunk, agent = pair
    try:
        return chunk, agent.name, agent.run(chunk.dossier, cg, embed_index, nova)
    except Exception as e:
        return chunk, f"{agent.name}:ERROR:{e}", []

with ThreadPoolExecutor(max_workers=4) as ex:     # keep low for Bedrock rate limits
    results = list(ex.map(_one, pairs))
```

Then regroup results per chunk into `ChunkReviewResult`. Pair this with the
adaptive retry config from A7 — parallelism without backoff will trip
throttling. The per-file loop in `run_review` and the per-change loop in
`run_dependency_check` are second-tier candidates for the same treatment.

### B2. `node_for_line` is O(all-graph-nodes) per changed line

**Location:** `pr_review/graph.py:455-468`

```python
def defs_in_file(self, path: str) -> List[str]:
    return [n for n, d in self.g.nodes(data=True)        # scans EVERY node
            if d.get("path") == path and d.get("kind") != "file"]

def node_for_line(self, path: str, line: int) -> Optional[str]:
    for n in self.defs_in_file(path):                    # called per line
```

**Why it matters:** `diff.map_changes` calls `node_for_line` for **every added
line** (`diff.py:94-97`); each call re-scans the entire graph. On a 5,000-file
repo with a 500-line PR that's 500 full-graph scans. `_outermost_defs`
(`context.py:299`) and `review.run_file_review` hit `defs_in_file` again.

**Fix:** Build the index once. `CodeGraph` already gets every def via
`_add_def`; add a `path -> [node_ids]` dict populated at build time (or a
`functools.lru_cache`-style lazy dict):

```python
# CodeGraph
_by_path: Dict[str, List[str]] = field(default_factory=dict)

def defs_in_file(self, path: str) -> List[str]:
    if path not in self._by_path:
        self._by_path[path] = [n for n, d in self.g.nodes(data=True)
                               if d.get("path") == path and d.get("kind") != "file"]
    return self._by_path[path]
```

(Same pattern fixes `routes()` / `tables()` if they ever show up hot.)

### B3. Verifier prompt is unbounded

**Location:** `pr_review/review.py:244`

```python
combined = "\n\n---\n\n".join(c.dossier for c in chunks)
```

**Why it matters:** Each chunk dossier is up to `token_budget` (12k default)
tokens. A large file split into 8 chunks produces a ~96k-token verifier prompt
— beyond what Nova Pro handles reliably, and at best it degrades verifier
quality (the "reject if not grounded" instruction over a 100k haystack).

**Fix:** Cap it with the existing `_toks` helper — e.g. keep whole chunk
dossiers until ~3× `token_budget` then stop, or run the verifier per-chunk
against only that chunk's candidate findings (cleaner: candidates already
carry file/line, so they can be grouped back to their chunk).

### B4. `_covering_tests` scans all nodes per change, with a substring trap

**Location:** `pr_review/blast.py:74-83`

```python
simple = cg.node(node_id).get("name", "")
for n, d in cg.g.nodes(data=True):                    # full scan per change
    if d.get("is_test") and simple.lower() in d.get("name", "").lower():
        tests.add(n)
```

Two issues: (a) full graph scan for every changed node — precompute the list of
test nodes once per `blast_radius` call; (b) substring matching means a
function named `run` "is covered" by `test_runner_setup` — false confidence
that lowers the `changes_without_tests` risk metric. Require word-ish matches
(e.g. `re.search(rf"(^|_){re.escape(simple.lower())}(_|$)", test_name)`).

---

## C. Dead code

| Location | What | Action |
|---|---|---|
| `pr_review/context.py:109-231, 236-278` | `build_chunk_dossier`, `make_chunks`, `build_all_chunk_dossiers` — the legacy proximity-chunk pipeline. Nothing calls them since the whole-file review (`make_file_chunks`) landed. ~150 lines, including the module docstring at the top which still *describes the old strategy*. | Delete (or move to a `legacy_` module if you want to keep the option); rewrite the module docstring to describe whole-file chunking. |
| `pr_review/review.py:36-38` | `build_dossier` and `Chunk` imported but `build_dossier` never used in this module (the Streamlit app imports it directly). | Drop from the import. |
| `pr_review/graph.py:251-253` | `extra["overrides_in"] = parent_class` is set **after** `_add_def` already copied `extra` into node attrs → the key is silently lost. Overrides are actually resolved later by `_resolve_overrides`, so this is dead, misleading code. | Delete the two lines. |
| `pr_review/graph.py:287-288` | `_extract_calls_in_body_import` — empty stub returning `[]`, never called. | Delete. |
| `pr_review/graph.py:312-313` | `_handle_call` — `pass` stub; `_visit` routes `call` nodes here so module-level calls are intentionally ignored, but the stub suggests unfinished work. | Delete the method and the `elif node.type in ("call",)` branch (the final `else` already recurses). |
| `pr_review/profiles.py:30-31` | `ReviewProfile.cross_file` — comment admits "reserved/unused". The Quick tier description says "No cross-file work" but the flag does nothing; only `caller_compat` matters. | Remove the field, or actually wire it (it was meant for the now-dead `build_chunk_dossier(cross_file=)` path — another argument for C-row-1 cleanup). |
| `pr_review/agents.py:381-388` | `ALL_AGENTS` module-level instances — only used as the `agents=None` fallback in `run_file_review`; `profiles.build_agents` + `AGENT_REGISTRY` is the real path. Two sources of truth. | Derive one from the other: `ALL_AGENTS = [cls() for cls in AGENT_REGISTRY.values()]`. |
| `pr_review/review.py:56` / `context.py:46-47` | `_toks` defined twice (`review.py:300` too) with the same body. | Keep one (export from `context`). |

---

## D. Repo hygiene

### D1. No `.gitignore` — compiled artifacts are committed

`git status` shows `claude-pr/__pycache__/*.pyc` and
`claude-pr/pr_review/__pycache__/*.pyc` tracked/untracked, plus a `venv/` at the
repo root. Add `.gitignore` at the repo root:

```gitignore
__pycache__/
*.py[cod]
venv/
.venv/
.streamlit/secrets.toml
*.egg-info/
```

and untrack what's already in:

```bash
git rm -r --cached claude-pr/__pycache__ claude-pr/pr_review/__pycache__
```

### D2. No `requirements.txt`

`streamlit_app.py`'s docstring says `pip install -r requirements.txt`, but the
file doesn't exist. Based on actual imports:

```text
streamlit
boto3
requests
networkx
numpy
tree-sitter
tree-sitter-python
tree-sitter-javascript
tree-sitter-java
tree-sitter-go
# optional extras
sentence-transformers   # embeddings (Deep tier)
neo4j                   # graph persistence
```

### D3. No tests at all

The eval harness (`eval.py`) needs AWS + labelled data, but a large share of the
codebase is **pure, deterministic logic** that's trivially unit-testable.
Highest-value pytest targets:

- `diff.parse_diff` — hunk numbering, new/deleted files, the `\ No newline`
  case (A5 regression test), multi-file diffs.
- `diff.map_changes` / `old_signature_for` — change-type classification
  (added / signature / behavior).
- `context.make_file_chunks` / `_outermost_defs` / `_split_range` — budget
  packing, nested-def dropping, oversized-def windowing.
- `filters.should_review` — lock files, vendored dirs, manifest opt-in.
- `identity._signature_from_source` / `_docstring` / `_return_hint` —
  multi-line Python defs, brace-style JS.
- `llm._extract_json` — fenced, bare, prefixed/suffixed JSON.
- `graph.build_graph` on a tiny fixture repo — node kinds, call edges,
  route/table detection.

---

## E. Smaller improvements

1. **Creds from environment** — `streamlit_app.py:57,141-144`: let the PAT
   default to `os.environ.get("GITHUB_TOKEN", "")` and leave AWS to the default
   boto3 chain (it already does when the fields are blank). Typing secrets into
   text inputs every session is friction and ends up in screen recordings.

2. **Tarball extraction on old Pythons** — `pr_review/github_client.py:117-121`:
   the `TypeError` fallback calls `tf.extractall(dest)` with **no filter** on
   Python < 3.12, which permits path traversal from a malicious archive. Fine
   for GitHub-only sources, but cheap to guard: reject members whose path
   starts with `/` or contains `..` before extracting.

3. **Dedup key too lossy** — `pr_review/review.py:254`:
   `key = (f.file, f.line, f.title[:40])` merges two *different* findings from
   different agents that happen to share a line and a title prefix. Include
   `f.category` in the key.

4. **Severity not validated** — `pr_review/agents.py:195`:
   `Finding.from_dict` lowercases severity but accepts anything; an off-schema
   value like `"major"` sorts to the end (`order.get(sev, 9)`) and renders as an
   unknown badge. Clamp: `if severity not in {"critical","high","medium","low","info"}: severity = "medium"`.

5. **`agents: list = None` typing** — `review.py:216,426` and `eval.py:211`:
   should be `Optional[List[BaseAgent]] = None` for clarity/mypy.

6. **Docstring regex grabs any string** — `pr_review/identity.py:93`:
   `re.search(r'"""(.*?)"""', src)` matches the first triple-quoted string
   anywhere in the body, not necessarily the docstring (e.g. a SQL constant).
   Anchor it to the def: search only the first ~10 lines after the signature,
   or use `ast.get_docstring` for Python sources.

7. **`find_similar` top_k unvalidated** — `pr_review/agents.py:142`:
   `int(inputs.get("top_k", 4))` accepts 0/negative from the model →
   `results[:0]`. Clamp to `max(1, min(top_k, 10))`.

8. **`_EmptyImpact` placement** — `review.py:296-297`: defined *after* its use
   in `run_file_review` and shadows the real `Impact` semantics with a
   class-level `tests: set = frozenset()`. `blast.Impact()` already exists and
   is the obvious default: `blast.per_change.get(ch.node_id, Impact()).tests`.

9. **TypeScript uses the JavaScript grammar** — `pr_review/graph.py:46-47`:
   `tree-sitter-typescript` exists and parses TS/TSX properly (type annotations
   currently make the JS grammar mis-parse some constructs, silently dropping
   defs). Try it first, fall back to JS.

10. **Quick profile never verifies** — `profiles.py:45` sets `verify=False` for
    Quick, but Quick also has *no* cross-file context, which is when agents
    hallucinate most. Consider verify-on for all tiers (it's one extra call per
    file) or at least document the noise trade-off in the UI caption.

11. **`_resolve` fans out to every same-name candidate** — `graph.py:526-535`:
    a call to `get()` adds edges to *every* `get` in the repo when no same-file
    match exists, inflating fan-in/blast metrics. Consider capping candidates
    (e.g. ≤3) or weighting same-package matches — worth a comment either way,
    since `_risk` consumes those counts.

12. **`requests` rate-limit detection is fragile** — `github_client.py:51`:
    GitHub signals rate-limiting via `403`/`429` with the
    `x-ratelimit-remaining: 0` header; matching `"rate limit" in r.text.lower()`
    misses localized/secondary-limit responses. Check the header instead.

13. **Progress callback granularity** — `streamlit_app.py:335-340`: progress
    jumps file-by-file; with B1's parallelism, switch to counting completed
    (chunk × agent) futures for a smooth bar.

14. **`format_report` severity summary ignores breaking findings' severities**
    — `review.py:511-515` counts only per-file issues; breaking-change findings
    (always high/critical) aren't in the `Critical:/High:` counts, which can
    read as "0 critical" above a section full of critical breaking changes.
    Either include them or label the counts "in-file".
