# How to run

Two stages, run in order:

1. **Index** (Phase 1) — build the deterministic structural graph in Neo4j. No LLM.
2. **Semantic** (Phase 2) — annotate that graph with meaning via an LLM. Needs an API key.

Two ways to run the indexer: **local** (fastest for dev) or **Docker** (isolated, reproducible).
Architecture: [`ARCHITECTURE.md`](ARCHITECTURE.md) · current state: [`STATUS.md`](STATUS.md) ·
semantic design: [`SEMANTIC_LAYER.md`](SEMANTIC_LAYER.md).

## Local

```bash
cd graph_rag
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# precise Python resolution (optional but recommended)
npm install --prefix scip_tooling @sourcegraph/scip-python@0.6.6

# Neo4j
docker run -d --name cbrain-neo4j -p7474:7474 -p7687:7687 \
  -e NEO4J_AUTH=neo4j/testpassword neo4j:5

# index a repo
NEO4J_PASSWORD=testpassword ./.venv/bin/python -m graph_rag.cli index <path> --repo NAME
```

Flags: `--no-wipe` (append instead of replacing the repo), `--no-scip` (heuristic only),
`--validation-report FILE`, `--fail-on-validation-error`.

Browse the graph at <http://localhost:7474> (`neo4j` / `testpassword`).

## Docker

Deps are baked into the image, so runs never hit a package registry. Set the repo to index in
`.env` (`REPO_PATH` = host path: `../sail` on macOS/Linux, `D:/Downloads/sail` on Windows).

```bash
docker compose build            # one-time; needs network (apt/PyPI/npm)
docker compose up neo4j -d
docker compose run --rm indexer
```

## Semantic enrichment (Phase 2)

Adds **meaning** on top of the indexed graph. Run `index` first. Config is read from
`.env` (see [Config](#config-env-vars)), so with `REPO_PATH` / `REPO_NAME` / a provider key
set, you can run it with no arguments. Flags override `.env` per run.

```bash
cd graph_rag

# Phase 2A — identity for every node, bottom-up (Function -> Class -> Package -> Repository).
# Identity is the small, cheap retrieval document; generated for everything.
./.venv/bin/python -m graph_rag.cli semantic                      # all from .env
./.venv/bin/python -m graph_rag.cli semantic <path> --repo NAME   # or explicit

# Preview the exact prompt the model would get — no API call, no cost (still needs Neo4j).
./.venv/bin/python -m graph_rag.cli semantic --dry-run --limit 1

# Smoke test: generate just a few, then inspect in Neo4j.
./.venv/bin/python -m graph_rag.cli semantic --limit 5

# Phase 2B — typed implementation flows (the "algorithm"). Larger; generate LAZILY / on demand,
# not for the whole repo. Each step is typed and references real graph node ids.
./.venv/bin/python -m graph_rag.cli semantic --flows --limit 50
```

Re-runs are cheap: a node whose body hasn't changed (`body_hash`) is **skipped**. Use
`--refresh` to regenerate anyway.

### Flags

| Flag | Meaning |
|---|---|
| `path` | repo root (same one used at `index` time; default: `REPO_PATH` from `.env`) |
| `--repo NAME` | graph repo tag (default: `REPO_NAME`, else dir name) |
| `--provider` | `anthropic` (default) · `bedrock` · `openai` |
| `--model` | model id (default: the provider's default, or `GRAPH_RAG_LLM_MODEL`) |
| `--levels` | subset of `function,class,package,repository` (default: all) |
| `--source` | raw-code inclusion: `auto` (only when no docstring, default) · `never` · `always` |
| `--flows` | Phase 2B: generate implementation flows instead of identities |
| `--limit N` | cap generations (smoke test / batch) |
| `--refresh` | regenerate even if `body_hash` is unchanged |
| `--dry-run` | print the assembled prompt; no API calls |

### Providers

Pick with `GRAPH_RAG_LLM_PROVIDER` in `.env` (or `--provider`) and set the matching credential:

| Provider | Install | Credentials | Default model |
|---|---|---|---|
| `anthropic` | (in `requirements.txt`) | `ANTHROPIC_API_KEY` | `claude-opus-4-8` |
| `bedrock` | `pip install 'anthropic[bedrock]'` | AWS creds + `AWS_REGION` | `anthropic.claude-opus-4-8` |
| `openai` | `pip install openai` | `OPENAI_API_KEY` | `gpt-4o` |

### Reading the results (Cypher, <http://localhost:7474>)

```cypher
// identities
MATCH (f:Function {repo:'sail'}) WHERE f.identity IS NOT NULL
RETURN f.fqn, f.identity, f.semantic_confidence LIMIT 20;

// which functions still need a flow (Phase 2B is lazy)
MATCH (f:Function {repo:'sail'}) WHERE f.semantic_state = 'IDENTITY_ONLY' RETURN count(f);

// typed-flow queries WITHOUT parsing JSON — uses the flat overlay props
MATCH (f:Function {repo:'sail'}) WHERE 'external_api' IN f.flow_step_types RETURN f.fqn;
// reverse: which flows reference a given node id
MATCH (f:Function {repo:'sail'}) WHERE 'ab12cd34...' IN f.flow_references RETURN f.fqn;
```

Each enriched node carries: `identity`, `semantic_confidence`, `semantic_model`,
`semantic_provider`, `semantic_version`, `semantic_state` (`IDENTITY_ONLY` | `FULL`),
`semantic_hash`; and once a flow exists: `implementation_flow_json` (the full typed steps),
`implementation_flow` (flat descriptions), `flow_step_types`, `flow_references`.

## Other commands

```bash
# web UI — add a repo, browse symbols/deps/source
NEO4J_PASSWORD=testpassword ./.venv/bin/python -m uvicorn webapp.server:app --port 8000

# resolver coverage probe (no DB)
./.venv/bin/python measure_coverage.py <path> --repo NAME

# is SCIP installed & working on this machine?
./.venv/bin/python scip_check.py
```

## Config (env vars)

A repo-root `.env` is loaded automatically (via `python-dotenv`) for both stages, so you can keep
config there instead of on the command line. Real env vars still win if set.

- **Indexer / Neo4j:** `NEO4J_URI` · `NEO4J_USER` · `NEO4J_PASSWORD` · `NEO4J_DATABASE` ·
  `REPO_PATH` · `REPO_NAME` · `SCIP_PYTHON_BIN`.
- **Semantic (Phase 2):** `GRAPH_RAG_LLM_PROVIDER` · `GRAPH_RAG_LLM_MODEL` ·
  `GRAPH_RAG_SOURCE_MODE` · and the provider key (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
  `AWS_REGION`).

## Troubleshooting
- **SCIP silently skipped** (graph uses heuristic CALLS): run `python scip_check.py`.
  - *Install fails behind a proxy (Zscaler):* `npm install` is blocked — install on an open
    network / pin `0.6.6`, or set `npm config set cafile <root.pem>`. Running needs no network.
  - *Windows "installed but unused":* npm makes `scip-python.cmd`; `config.py` now finds it.
  - *Non-git repo:* handled (`--project-version` is passed automatically).
- **Neo4j auth/connection error:** confirm the container is up (`docker start cbrain-neo4j`) and
  `NEO4J_PASSWORD` matches what the container was first initialized with.
- **scip-java / Java CALLS:** Java stays on the heuristic resolver (scip-java needs Maven/Gradle).
- **Semantic: "set ANTHROPIC_API_KEY…":** the provider key is missing — put it in `.env` (or the
  matching `OPENAI_API_KEY` / `AWS_REGION` for the chosen provider).
- **Semantic: "no repo path":** set `REPO_PATH` in `.env` or pass the path argument.
- **Semantic provider SDK missing:** `pip install openai` (openai) or
  `pip install 'anthropic[bedrock]'` (bedrock); `anthropic` is already in `requirements.txt`.
- **Everything says `cached`:** bodies are unchanged since the last run — that's the freshness
  cache working. Use `--refresh` to force regeneration.
