# How to run

Two ways: **local** (fastest for dev) or **Docker** (isolated, reproducible).
Architecture: [`ARCHITECTURE.md`](ARCHITECTURE.md) · current state: [`STATUS.md`](STATUS.md).

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
`NEO4J_URI` · `NEO4J_USER` · `NEO4J_PASSWORD` · `NEO4J_DATABASE` · `SCIP_PYTHON_BIN`.

## Troubleshooting
- **SCIP silently skipped** (graph uses heuristic CALLS): run `python scip_check.py`.
  - *Install fails behind a proxy (Zscaler):* `npm install` is blocked — install on an open
    network / pin `0.6.6`, or set `npm config set cafile <root.pem>`. Running needs no network.
  - *Windows "installed but unused":* npm makes `scip-python.cmd`; `config.py` now finds it.
  - *Non-git repo:* handled (`--project-version` is passed automatically).
- **Neo4j auth/connection error:** confirm the container is up (`docker start cbrain-neo4j`) and
  `NEO4J_PASSWORD` matches what the container was first initialized with.
- **scip-java / Java CALLS:** Java stays on the heuristic resolver (scip-java needs Maven/Gradle).
