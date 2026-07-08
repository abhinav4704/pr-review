# GRAPH.md — how the code graph is built (simple version)

See [AGENT_INPUTS.md](AGENT_INPUTS.md) for what each agent is shown.

## 1. Build order

- `index_repo` — reads code, creates all nodes + edges (only stage that creates them)
- `enrich_identities` — LLM adds `identity` text to every node
- `generate_flows` — LLM adds `implementation_flow` to functions (only when needed, not all at once)
- `run_taint_pass` — no LLM, adds `taint_json` to functions (where tainted data can flow to)
- `tag_sanitizers` — LLM, adds `sanitizer_json` (which functions clean/validate data)
- `find_taint_findings` — no LLM, walks the facts above to find source→sink chains
- **4 agents** — read the graph, produce findings (findings are NOT written back to the graph)
- `score_findings` — ranks findings by severity + blast radius

Example: run in order for a repo called `shop`:
```
index      -> creates Function nodes for shop/orders.py etc.
semantic   -> Function "place_order" gets identity: "Creates a new order and charges the customer."
semantic --flows -> same function gets implementation_flow: ["validation", "database_write", "external_api"]
taint      -> same function gets taint_json: reaches a SQL sink at line 40
architecture -> Agent 4 reads identity+flow, flags a layering issue
```

## 2. Node types (11 total)

- `Repository` — one per repo
- `Package` — a folder/namespace
- `File` — one source file
- `Module` — language-level module grouping
- `Class` — a class/interface
- `Function` — a method/function (most properties live here)
- `Field` — a class/instance variable
- `Annotation` — a decorator (e.g. `@app.route`)
- `Endpoint` — an HTTP route (method + path)
- `Event` — a pub/sub topic/queue
- `Policy` — an auth rule marker

Example: `def get_order(order_id): ...` inside `class OrderController` in `controllers.py` becomes:
- 1 `File` node for `controllers.py`
- 1 `Class` node for `OrderController`
- 1 `Function` node for `get_order`
- an edge `Class -CONTAINS-> Function`

## 3. Node ID — how a node is identified

- `id = sha1(repo + kind + fqn)`, shortened to 16 chars
- NOT based on line numbers
- Example: renaming a variable inside `get_order` changes its `body_hash`, but `id` stays the same → re-indexing updates it in place instead of duplicating it

## 4. What properties a node can have

### Always present (from indexing)
- `name`, `fqn`, `file`, `signature`, `docstring`
- `start_line`, `end_line`
- `param_names`, `param_types`, `return_type`
- `loc`, `cyclomatic`, `fan_in`, `fan_out`
- `body_hash` — changes whenever the code inside changes

Example for `get_order`:
```
name: "get_order"
fqn: "controllers.OrderController.get_order"
signature: "def get_order(order_id: int) -> Order"
param_names: ["order_id"]
```

### `component_role` — what kind of code this is
- `controller` — e.g. class ends in `Controller`, or has `@RestController`
- `service` — e.g. class ends in `Service`
- `repository` — e.g. class ends in `Repository`/`Dao`
- `entity` — e.g. has `@Entity`
- `config` — e.g. class ends in `Config`
- `util` — e.g. class ends in `Util`/`Utils`
- `endpoint_handler` — a Function that actually serves an HTTP route (has an `EXPOSES` edge, or inherits it from a `controller` class)

Example: `OrderController.get_order` → `component_role = endpoint_handler`

### Semantic properties (LLM-generated)
- `identity` — 1-2 sentence plain-English summary
- `keywords` / `tags` / `concepts` — for search (e.g. tags: `["api", "persistence"]`)
- `implementation_flow` — ordered list of steps, e.g.:
  ```
  ["validation: checks order_id is valid",
   "database_read: fetches order from OrderRepository",
   "response: returns order as JSON"]
  ```

### Taint properties (security data-flow)
- `taint_json` — where this function's inputs can leak to, e.g.:
  ```json
  {"sinks": [{"vuln_class": "sql_injection", "callee": "conn.execute", "line": 40, "from_params": [0]}]}
  ```
  (means: parameter 0 reaches a SQL sink at line 40)
- `sanitizer_json` — which params this function cleans, e.g.:
  ```json
  {"sql_injection": [0]}
  ```
  (means: this function sanitizes parameter 0 against SQL injection)

## 5. Edge types (the important ones)

- `CONTAINS` — structural nesting (File contains Class contains Function)
- `CALLS` — function A calls function B
- `READS` / `WRITES` — function reads/writes a shared Field
- `EXPOSES` — function serves an HTTP Endpoint
- `THROWS` / `CATCHES` — exception handling
- `EXTENDS` / `IMPLEMENTS` — class inheritance
- `IMPORTS` — file imports another module
- `PASSES` — argument passed from caller to callee (used for taint composition)

Example:
```
controllers.get_order_endpoint -CALLS-> services.get_order
services.get_order -CALLS-> repository.fetch_order
repository.fetch_order -PASSES(order_id)-> conn.execute   (this is the sink)
```

### Edge properties
- `confidence` — `EXTRACTED` (certain), `INFERRED` (heuristic guess), `AMBIGUOUS` (multiple matches)
- `origin` — `EXTRACTED` (from code) or `DERIVED` (computed later)
- `evidence_file` / `evidence_line` — where in the source this edge came from

## 6. How a name becomes a real edge

- Code says `foo(x)` → extractor first records it as "a call named foo, unresolved"
- Resolver looks up `foo` in the repo's own symbol table
- Found exactly one match → `CALLS` edge, `confidence = EXTRACTED` (or better, via SCIP)
- Found by heuristic name-matching only → `confidence = INFERRED`
- Multiple possible matches → `confidence = AMBIGUOUS`
- Not found in-repo (e.g. a library call) → marked `external`, may be dropped

## 7. What's NOT stored in the graph

- Agent findings (security bugs, etc.) — those go to `output/*.json`, not the graph
- Vector embeddings — optional, separate `embed` step, only if you ask for it
- No line-number-based IDs — line numbers are just metadata, editable without breaking anything

## 8. Simple picture

```
Repository
  -> Package / File
       -> Class            (component_role: controller / service / repository ...)
            -> Function     (component_role: endpoint_handler if it serves a route)
                 -> CALLS other functions
                 -> READS / WRITES fields
                 -> has identity, implementation_flow, taint_json, sanitizer_json
```
