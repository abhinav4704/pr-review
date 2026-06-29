# How to use the Codebase Brain

A practical guide: set up, index a repo, browse it in the web UI, and query the graph
directly. Everything here is **deterministic** — no LLM is involved yet.

> New here? Read [`STATUS.md`](STATUS.md) for what the system is and the current
> architecture. This file is just the operating manual.

---

## 0. Prerequisites (one-time setup)

All commands run from the `graph_rag/` directory.

### a) Python env
```bash
cd graph_rag
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

### b) Neo4j (graph database) in Docker
```bash
# first time:
docker run -d --name cbrain-neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/testpassword \
  -e NEO4J_PLUGINS='["graph-data-science"]' \
  neo4j:5

# after a reboot, just restart it (data persists):
docker start cbrain-neo4j
```
- Bolt (driver): `bolt://localhost:7687` · Browser UI: http://localhost:7474
- Login: `neo4j` / `testpassword`
- Override via env: `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`.

### c) scip-python (precise Python call resolution) — recommended
```bash
npm install --prefix scip_tooling @sourcegraph/scip-python@0.6.6
```
Without it, indexing still works but Python `CALLS` fall back to the (less precise)
heuristic resolver. Java currently always uses the heuristic (scip-java needs maven/gradle).

---

## 1. Index a repository (CLI)

```bash
NEO4J_PASSWORD=testpassword ./.venv/bin/python -m graph_rag.cli index <path-to-repo> --repo NAME
```

Examples:
```bash
# index the sample Python repo
NEO4J_PASSWORD=testpassword ./.venv/bin/python -m graph_rag.cli index ../primitive-pr --repo primitive-pr
```

Flags:
- `--repo NAME` — name to store under (defaults to the directory name). Multiple repos
  coexist in one database; queries/filters are per-repo.
- `--no-wipe` — append instead of replacing this repo's existing nodes.
- `--no-scip` — skip SCIP; use only the heuristic resolver.

What you'll see: file/node/edge counts, the SCIP result line (e.g. `EXTRACTED via
scip-python — 415 edges`), and the heuristic resolution-coverage table.

---

## 2. Browse it in the web UI (easiest)

```bash
NEO4J_PASSWORD=testpassword ./.venv/bin/python -m uvicorn webapp.server:app --port 8000
# open http://localhost:8000
```

In the browser:
1. **Pick a repo** in the top dropdown (the sample `primitive-pr` is auto-listed).
2. **Add a new repo** with **“+ Add repo”** → paste a **GitHub URL** (it shallow-clones and
   indexes) or **upload a `.zip`** of a repo. Indexing runs automatically.
3. **Search** functions / classes in the left panel (toggle fn/class/file/field).
4. **Click a node** to see, on the right:
   - **metadata** (visibility, return type, params, modifiers, static/async, signature),
   - **dependencies →** (CALLS, RETURNS, HAS_TYPE, OF_TYPE, HAS_GENERIC, CONTAINS …) and
     **← used by**, each tagged with confidence (EXTRACTED/INFERRED/AMBIGUOUS) and
     provenance (`file:line` + which extractor produced it). Click any dependency to jump.
   - **source code** — the real lines for that function/class.

See [`webapp/README.md`](webapp/README.md) for the API endpoints.

---

## 3. Query the graph directly (Neo4j Browser / Cypher)

Open http://localhost:7474 and run Cypher. Every node shares the label `:CodeNode`
(+ a specific label: `File` / `Class` / `Function` / `Field` / `Annotation`). Filter by
`repo`.

**Find a function**
```cypher
MATCH (f:Function {repo:'primitive-pr'}) WHERE f.name = 'build_graph' RETURN f;
```

**What does it call? (direct dependencies)**
```cypher
MATCH (f:Function {repo:'primitive-pr', name:'build_graph'})-[:CALLS]->(callee)
RETURN callee.fqn, callee.file, callee.start_line;
```

**Blast radius — who transitively calls X (impact of changing it)**
```cypher
MATCH (f:Function {repo:'primitive-pr'}) WHERE f.fqn ENDS WITH 'confidence_score'
MATCH (caller)-[:CALLS*1..3]->(f)
RETURN DISTINCT caller.fqn;
```

**Where is a type used?**
```cypher
MATCH (u)-[r:RETURNS|OF_TYPE|HAS_TYPE|HAS_GENERIC]->(t:Class {repo:'primitive-pr', name:'Finding'})
RETURN u.fqn, type(r), r.evidence_file, r.evidence_line;
```

**Class hierarchy**
```cypher
MATCH (c:Class {repo:'primitive-pr'})-[:EXTENDS|IMPLEMENTS]->(parent)
RETURN c.name, parent.name;
```

**Provenance / trust of an edge** (how it was resolved + where the evidence is)
```cypher
MATCH ()-[r:CALLS]->() 
RETURN r.confidence, r.origin, r.extractor, count(*) ORDER BY count(*) DESC;
```

**Confidence model:** `EXTRACTED` = precise (SCIP, or directly observed) · `INFERRED` =
heuristic name match · `AMBIGUOUS` = multiple candidates. `origin` = `EXTRACTED` (read from
source) vs `DERIVED` (computed later).

---

## 4. Check resolution quality without a database (dev probe)

```bash
./.venv/bin/python measure_coverage.py ../primitive-pr --repo primitive-pr
```
Prints per-edge-type coverage, the heuristic call-resolution rate, and a receiver-shape
census — handy when tuning the resolver.

---

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| `ServiceUnavailable` / connection refused | Neo4j isn't running → `docker start cbrain-neo4j` |
| Auth error | wrong password → set `NEO4J_PASSWORD` to match the container |
| Web UI shows “⚠ no source” | repo's on-disk path isn't registered → re-add it via the UI |
| SCIP line missing / `available=False` | scip-python not installed (see 0c) or repo has no Python; heuristic is used |
| Python CALLS look wrong | run with SCIP (drop `--no-scip`); heuristic is ~85% precise, SCIP is exact |
| Java calls imprecise | expected — Java uses the heuristic until scip-java is wired |
| Re-index didn't remove old nodes | you passed `--no-wipe`; omit it to replace the repo |

---

## Quick reference

```bash
# index
NEO4J_PASSWORD=testpassword ./.venv/bin/python -m graph_rag.cli index <path> --repo NAME

# web UI
NEO4J_PASSWORD=testpassword ./.venv/bin/python -m uvicorn webapp.server:app --port 8000

# coverage probe (no DB)
./.venv/bin/python measure_coverage.py <path> --repo NAME

# neo4j lifecycle
docker start cbrain-neo4j        # browser at :7474, bolt at :7687
```
