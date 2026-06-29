# Codebase Analyser - Current Implementation (Detailed)

## 1. Purpose and Scope

The `codebase-analyser` project provides a deterministic, whole-repository audit workflow focused on:

- breakage-risk mapping ("what can break what")
- missing-symbol import detection
- dead-code candidate detection
- architecture signal extraction
- lightweight security scanning for possible secrets
- dependency hygiene checks (declared vs imported, optional OSV enrichment)

It is intentionally manual-first and currently implemented as a Streamlit app.

## 2. High-Level Design

### 2.1 Architectural strategy

The analyser does **not** build its own graph engine. It reuses the graph builder from sibling project `primitive-pr` through a strict adapter boundary.

Design goals implemented:

- keep `primitive-pr` as read-only dependency for graph construction
- run deterministic checks in-memory against one graph snapshot
- aggregate all checks into a single `AuditResult`
- expose results in interactive tabs and downloadable markdown/json reports

### 2.2 Runtime flow (current)

```mermaid
flowchart TD
    A[Streamlit UI: audit_frontend.py] --> B[run_audit(repo_root, depth, top_n, include_osv)]
    B --> C[build_analyzer_graph via adapter]
    C --> D[primitive-pr build_graph]
    D --> E[CodeGraph instance]

    E --> F[Breakage analysis]
    E --> G[Missing symbol extraction]
    E --> H[Dead-code analysis]
    E --> I[Architecture analysis]

    B --> J[Secrets scanner]
    B --> K[Dependency scanner]

    F --> L[Audit summary + health score]
    G --> L
    H --> L
    I --> L
    J --> L
    K --> L

    L --> M[UI tabs + metrics]
    L --> N[Markdown report]
    L --> O[JSON export]
```

## 3. Repository Structure (Current)

```text
codebase-analyser/
  analyser/
    __init__.py
    audit.py
    paths.py
    graph_adapter.py
    graph_contract.py
    extract_unresolved.py
    checks/
      common.py
      breakage.py
      deadcode.py
      architecture.py
    scanners/
      secrets.py
      dependencies.py
  audit_frontend.py
  tests/
    test_audit_smoke.py
    test_graph_contract_helpers.py
  PLAN.md
  DETAILED_IMPLEMENTATION_PLAN.md
  FUTURE.md
  requirements.txt
```

## 4. Entry Point and UI Layer

File: `audit_frontend.py`

### 4.1 UI behavior

The Streamlit page configures:

- repository root input
- breakage depth slider
- top-N risky nodes slider
- OSV lookup toggle
- Run Audit action

### 4.2 Execution path

When Run Audit is pressed:

1. Validate repo root exists
2. Execute `run_audit(...)`
3. Persist result in `st.session_state`
4. Render:
   - summary metrics (files, definitions, findings, health)
   - tabs for each analysis section
   - markdown/json download buttons

### 4.3 Output tabs

Current tabs:

- Overview
- Breakage
- Architecture
- Dead Code
- Security
- Dependencies
- Report

## 5. Core Orchestration (`analyser/audit.py`)

### 5.1 Data models

- `AuditSummary`
  - `repo_root`
  - `generated_at`
  - `files`
  - `definitions`
  - `findings_total`
  - `health_score`

- `AuditResult`
  - `summary`
  - `breakage`
  - `deadcode`
  - `architecture`
  - `security`
  - `dependencies`

### 5.2 `run_audit(...)`

Implemented sequence:

1. Build graph via `build_analyzer_graph(abs_root, backend="primitive")`
2. Run breakage analysis
3. Extract missing symbols and attach into breakage payload
4. Run dead-code analysis
5. Run architecture analysis
6. Scan secrets
7. Scan dependencies (optional OSV)
8. Count files and definitions
9. Compute `findings_total` and `health_score`
10. Return `AuditResult`

### 5.3 Health score function

`_compute_health_score(...)` starts from 100 and subtracts capped penalties from:

- breakage blast findings
- missing symbol findings
- dead-code candidates
- import cycles
- secrets findings
- dependency findings

Final score is clamped to `0..100`.

### 5.4 Report formatter

`format_audit_report(result)` generates markdown sections:

- metadata summary
- breakage + missing symbols
- dead code
- architecture
- security
- dependencies

This is what the UI downloads as `codebase_audit_report.md`.

## 6. Adapter Boundary to Primitive Graph

### 6.1 Path bootstrap (`analyser/paths.py`)

`ensure_primitive_on_path()`:

- locates sibling folder `primitive-pr`
- injects it into `sys.path`
- raises explicit `FileNotFoundError` if absent

### 6.2 Graph adapter (`analyser/graph_adapter.py`)

Responsibilities:

- import and call `pr_review.graph.build_graph(...)`
- enforce interface contract with `_validate_graph_interface`
- expose helper `count_files_and_definitions`

Required graph members validated:

- `g`
- `node`
- `fan_in`
- `reverse_dependents`
- `routes`
- `events`

This keeps analysis modules decoupled from backend internals.

## 7. Graph Contract Helpers

File: `analyser/graph_contract.py`

Provides normalization helpers consumed by checks:

- `node_kind(...)`
- `node_name(...)`
- `edge_relation(...)`
- `is_import_edge(...)`
- `is_file_node(...)`
- `is_definition_node(...)`

The analyser treats the following as definition kinds:

- function
- method
- class
- route
- event
- table

## 8. Deterministic Checks

## 8.1 Shared filters (`checks/common.py`)

`should_exclude_path(path)` filters paths containing segments such as:

- tests/test
- `__pycache__`
- node_modules
- dist/build
- vendor/generated
- venv/.venv

Used across checks/scanners to reduce noise.

## 8.2 Breakage (`checks/breakage.py`)

Goal: estimate blast radius of changing important nodes.

### Candidate selection

`_candidate_nodes(code_graph)`:

- includes node kinds: function/method/route/event/class
- excludes filtered paths
- ranks by:
  - fan-in
  - entrypoint bonus (if route/event)
  - sensitive-name bonus using token set:
    - auth, token, secret, password, billing, payment, session, permission, admin

### Blast computation

`analyze_breakage(code_graph, depth, top_n)`:

- take top `top_n` candidates
- call `reverse_dependents(node_id, depth, exclude_ambiguous=True)`
- build impacted dependents list with hops/kind/path/name
- assign severity by impacted count:
  - high: >=15
  - medium: >=6
  - low: otherwise
- also provide hotspot table (top fan-in nodes)

Returned payload keys:

- `blast_radius`
- `hotspots`

## 8.3 Missing Symbol Extraction (`extract_unresolved.py`)

Goal: identify in-repo `from X import Y` where `Y` is not defined in module `X`.

Main steps:

1. Build module->file index from all `.py` files
2. Build definitions-by-file map from graph nodes
3. Parse each Python file AST
4. Evaluate `ast.ImportFrom` nodes
5. Resolve relative imports to absolute module names
6. For each imported symbol:
   - skip wildcard imports
   - skip builtins
   - if symbol absent in target module definitions, emit finding

Finding shape includes:

- severity
- source path and line
- module and symbol
- target file
- explanatory message

Sorter: path, line, symbol.

## 8.4 Dead Code (`checks/deadcode.py`)

Goal: conservative orphan candidate detection.

`analyze_deadcode(code_graph, max_results=200)` flags nodes when:

- kind is function or method
- node is not test
- path not excluded
- node is not route/event entrypoint
- fan_in == 0

Output key:

- `orphans` (sorted by path/start_line/name)

## 8.5 Architecture (`checks/architecture.py`)

Goal: structural repository signals from graph topology.

### Import graph

`_import_graph(code_graph)` creates file-only directed graph where edge relation is `imports`.

### Signals extracted

`analyze_architecture(code_graph)` returns:

- `kind_counts` of all node kinds
- `import_cycles` (up to 20 simple cycles)
- `module_coupling` (cross-module import edge counts)
- `hotspots` (top fan-in definition nodes)
- `entrypoints` (routes + events)

`_module_coupling` groups by top-level folder/module segment.

## 9. Scanners

## 9.1 Secrets scanner (`scanners/secrets.py`)

Checks text-like files for potential secrets.

### Coverage

Scanned suffixes include:

- `.py`, `.js`, `.ts`, `.tsx`, `.jsx`
- `.json`, `.yml`, `.yaml`, `.env`, `.ini`, `.toml`, `.md`

### Detection methods

1. Pattern-based (high severity), examples:
   - assignments containing api_key/secret/token/password
   - AWS access-key style
   - private key headers
   - GitHub PAT format

2. Entropy heuristic (medium severity):
   - long token pattern
   - Shannon entropy above threshold (default 3.8)

False-positive dampening:

- skip lines with hints like `example`, `sample`, `dummy`, `test`, `placeholder`

Returns `{ "findings": [...] }` sorted by path/line/type.

## 9.2 Dependency scanner (`scanners/dependencies.py`)

Compares declared dependencies and imported modules for Python.

### Declared packages

From `requirements.txt` only (current implementation).

### Imported modules

AST scan over `.py` files:

- `import X`
- `from X import ...`

Normalize names to lowercase with `_` -> `-`.

### Noise reduction

Treat local module stems as local, then compute imported external set.

### Finding classes

- `undeclared`: imported but not declared
- `unused`: declared but not imported
- optional OSV vulnerability findings when enabled

### OSV behavior

`include_osv=True` triggers query to `https://api.osv.dev/v1/query` per declared package.

`osv_status` values:

- `disabled`
- `ok`
- `offline-or-failed`

## 10. Primitive Graph Backend (Used by Analyser)

The analyser relies on these semantics from `primitive-pr/pr_review/graph.py`:

- `build_graph(root, backend="primitive")` builds the graph
- `CodeGraph.g` is a NetworkX directed graph
- `CodeGraph.node(nid)` returns node attributes
- `CodeGraph.fan_in(nid)` returns inbound caller count
- `CodeGraph.reverse_dependents(...)` performs reverse BFS over configured relation set
- `CodeGraph.routes()` and `events()` expose entrypoint node ids

The backend supports multi-language extraction and a broad edge set, while analyser checks consume only the contract surface they require.

## 11. Test Coverage (Current)

### 11.1 `tests/test_audit_smoke.py`

Validates end-to-end behavior on temporary mini repos:

- smoke audit run and summary counts
- import cycle detection
- generated/tests path exclusion from dead-code candidates
- missing-symbol import detection
- secrets + dependency scanner behavior (with OSV disabled)

### 11.2 `tests/test_graph_contract_helpers.py`

Validates analyser compatibility with legacy/canonical edge fields:

- relation extraction from `type` and `relation`
- architecture import coupling behavior on a fake graph

## 12. How to Run (Current)

From `codebase-analyser/`:

```powershell
pip install -r requirements.txt
streamlit run audit_frontend.py
```

In UI:

1. Set repository root path
2. Select depth/top risky nodes/OSV toggle
3. Run Audit
4. Inspect tabs
5. Export markdown/json report

## 13. Current Constraints and Known Limits

- Manual UI workflow only; no dedicated CLI entrypoint in current implementation
- Dependency scanner currently reads only `requirements.txt` (no Poetry/pipenv/conda lock parsing)
- Missing-symbol extraction currently targets Python `ImportFrom` shape
- Secret detection is heuristic and can produce false positives/negatives
- Architecture cycles are capped (`simple_cycles` limited to first 20)
- Health score is a heuristic aggregate, not a calibrated quality metric

## 14. Non-Goals in Current State

- Not a PR-diff reviewer pipeline
- Not a mandatory LLM auditing stage
- Not enforcing CI gate behavior by default

## 15. Summary

The current implementation is a deterministic, graph-centered repository audit system with:

- reusable backend graph extraction from `primitive-pr`
- modular checks/scanners run in one orchestrated pass
- practical reporting through Streamlit and markdown/json exports

It is already usable for manual codebase diagnostics and risk surfacing, with future expansions planned in existing planning documents.
