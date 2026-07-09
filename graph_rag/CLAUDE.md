# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# Precise Python call resolution (optional but recommended)
npm install --prefix scip_tooling @sourcegraph/scip-python@0.6.6

# Neo4j
docker run -d --name cbrain-neo4j -p7474:7474 -p7687:7687 \
  -e NEO4J_AUTH=neo4j/testpassword neo4j:5
```

### Index, analyze, retrieve

```bash
# Phase 1 — structural graph (no LLM, ~seconds)
./.venv/bin/python -m graph_rag.cli index <path> --repo NAME

# 2-agent analysis: Agent A (correctness+impact) + Agent B (taint+architecture)
./.venv/bin/python -m graph_rag.cli analyze <path> --repo NAME
./.venv/bin/python -m graph_rag.cli analyze <path> --no-llm   # deterministic taint only

# Phase 2 — semantic enrichment (RAG tool only, not used by analyzer)
./.venv/bin/python -m graph_rag.cli semantic <path> --repo NAME          # identities
./.venv/bin/python -m graph_rag.cli semantic <path> --flows --limit 50   # flows (lazy)
./.venv/bin/python -m graph_rag.cli semantic --dry-run --limit 1         # preview prompt

# Phase 3 — embed + query
./.venv/bin/python -m graph_rag.cli embed --repo NAME
./.venv/bin/python -m graph_rag.cli ask "how does X work" --repo NAME

# Web UI (no LLM — browse graph, add repos, view source)
NEO4J_PASSWORD=testpassword ./.venv/bin/python -m uvicorn webapp.server:app --port 8000
```

### Validation / diagnostics

```bash
# Regression test: runs pipeline over fixtures/, asserts edge types still fire. Exit 1 on failure.
./.venv/bin/python validate_fixtures.py

# Resolver coverage probe (no DB)
./.venv/bin/python measure_coverage.py <path> --repo NAME

# Check SCIP is installed and functional
./.venv/bin/python scip_check.py
```

## Configuration

A `.env` file at the project root is auto-loaded by `graph_core/config.py`. Real env vars win over `.env`.

| Variable | Purpose |
|---|---|
| `NEO4J_URI/USER/PASSWORD/DATABASE` | Neo4j connection (defaults: `bolt://localhost:7687`, `neo4j`, `testpassword`, `neo4j`) |
| `REPO_PATH` / `REPO_NAME` | Default repo path and tag for `analyze`/`semantic`/`embed`/`ask` |
| `GRAPH_RAG_LLM_PROVIDER` | `anthropic` (default) · `bedrock` · `openai` |
| `GRAPH_RAG_LLM_MODEL` | Override the provider's default model |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `AWS_REGION` | Credential for the chosen provider |
| `GRAPH_RAG_EMBED_PROVIDER` | `local` (default, offline) · `voyage` · `openai` |
| `SCIP_PYTHON_BIN` | Override scip-python binary path |

## Architecture

The system is two separate tools that share a common structural backend:

```
graph_core/  — shared extraction, resolution, Neo4j storage
    ↓                            ↓
analyzer/                       rag/
(security/correctness review)   (RAG retrieval: semantic + embed + ask)
```

### graph_core — structural pipeline

`pipeline.py:index_repo()` is the single orchestration function:

1. **Discover** (`discovery.py`) — walk files, detect language.
2. **Extract** (`extractors/python.py`, `extractors/java.py`) — tree-sitter AST → raw `Node`/`Edge`/`RawRef` objects.
3. **Canonical IR** (`canonical_ir.py`) — normalize per-file extractor output before merge.
4. **Resolve** (`resolver.py`) — heuristic name-matching: turns `RawRef` (target name only) into resolved `Edge` (both node ids). Confidence: `EXTRACTED` for precise, `INFERRED` for heuristic, `AMBIGUOUS` for multi-match.
5. **SCIP resolution** (`scip_resolver.py`) — runs `scip-python` for type-precise Python `CALLS`; replaces heuristic Python CALLS when available. Java CALLS stays heuristic (scip-java needs Maven/Gradle build).
6. **Derive** — `OVERRIDES` from class hierarchy, `Repository`/`Package` containment tree, static metrics (fan-in/fan-out, cyclomatic), component roles (controller/service/repository/etc.), `Module` nodes, aggregate `USES` edges.
7. **Write** (`store.py`) — upsert nodes and edges into Neo4j via `MERGE`.

**Core data types** (`models.py`): `Node`, `Edge`, `RawRef`. Every node has a stable `id = hash(repo + kind + fqn)`. Every edge carries `confidence` (EXTRACTED/INFERRED/AMBIGUOUS) and `origin` (EXTRACTED/DERIVED) + evidence `file:line:col`.

**Security invariant in `store.py`:** node labels and edge types are validated against a schema allowlist (`assert_label`/`assert_edge`) before Cypher interpolation — Cypher can't parametrize these, so the allowlist is the injection guard.

### analyzer — 2-agent analysis

Reads source + graph directly. No identity/flow generation involved.

**Agent A** (`analyzer/agents.py`): correctness + impact. Chunks whole functions from a file, sends raw source + caller shapes (signature only, not bodies) + shared-state facts from graph. One LLM call per chunk.

**Agent B** (`analyzer/taint.py`), three passes:
1. Deterministic sink tagging + taint transfer-function extraction (tree-sitter, no LLM). Cached by `body_hash`.
2. LLM taint qualify: reads raw source of every function in the source→sink chain to confirm/deny.
3. Architecture/layering: Stage 1 collapses call chains from endpoint handlers into role-sequence "shapes" (one batched LLM call); Stage 2 deep-dives flagged shapes against raw source.

**Scoring** (`analyzer/scoring.py`): blast-radius traversal (`CALLS` graph closure) + base severity from `findings.SEVERITY_BASE` table + LLM qualify confirmation.

**Finding IDs** are keyed on `owning_fqn + category + subcategory + line` for stable suppression/baseline (not yet built). Subcategories must match the `SEVERITY_BASE` table in `findings.py` or severity silently defaults — add new subcategories to both `agents.py`'s vocabulary list and `findings.SEVERITY_BASE` together.

### rag — RAG tool (separate from analyzer)

**Semantic enrichment** (`rag/semantic.py`): two artifacts per node, cached by `body_hash`:
- **Identity** (`enrich_identities`) — generated bottom-up Function → Class → Package → Repository. A function's identity prompt uses only names of callees/callers/reads/writes, never their identity text — cache invalidation is shallow (only direct callers need refresh when a callee changes).
- **Flows** (`generate_flows`) — typed implementation-flow steps (`validation`, `database_read`, etc.), function-only, generated lazily.

**Embeddings** (`rag/embeddings.py`): embeds the identity document (not raw code) into a Neo4j HNSW vector index `code_embedding` (cosine, 384-dim by default with local `all-MiniLM-L6-v2`).

**Retrieval** (`rag/retrieval.py`): hybrid loop — vector search + keyword match on `identity_keywords/tags/concepts/name/fqn` → RRF fusion → LLM prune → graph neighbor expansion → LLM prune → context pack (target: full source; neighbors: signature + identity) → LLM answer. Each stage is recorded on `AskResult.stages`. Degrades gracefully with `--no-llm` (returns ranked structure, skips prune/answer).

**Design rule (from memory):** prune/dedup is always LLM-driven, never Python set-logic.

### webapp / frontend

- `webapp/` — FastAPI server: no LLM, no embeddings. Accepts GitHub URL or .zip, runs `index_repo`, exposes symbol search, dependency browser, and source view. Static files in `webapp/static/`.
- `frontend/` — Streamlit UI: GitHub → Neo4j → 4-agent analysis pipeline.

### scip_tooling

npm package dir vendoring `@sourcegraph/scip-python@0.6.6`. The binary is at `scip_tooling/node_modules/.bin/scip-python`. `config.py:scip_python_bin()` probes this path first, then `$SCIP_PYTHON_BIN`, then `PATH`.

## Key design rules

- **Analyzer never uses identity/flow** — it reads raw source + the structural graph directly. The `rag/` semantic layer is a separate concern.
- **No new edge types without a milestone** defining them. No duplicate/alias edges (e.g. no `DECLARES` aliasing `CONTAINS`).
- **Confidence + origin on every edge**, with evidence `file:line:col` — every claim must be traceable.
- **`validate_fixtures.py`** is the regression gate for extraction/resolution: run it after touching any extractor, resolver, or derived-edge logic.
