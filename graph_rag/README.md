# graph_rag — Codebase Brain graph builder

Builds a **Neo4j knowledge graph** of a Java/Python codebase: every symbol becomes a node,
every dependency a typed, confidence-tagged, provenance-tracked edge. Deterministic — no LLM.

- **Full operating manual:** [`HOWTOUSE.md`](HOWTOUSE.md)
- **Architecture + roadmap + progress:** [`STATUS.md`](STATUS.md) · design ref: [`../ARCHITECTURE.md`](../ARCHITECTURE.md)
- **Web UI:** [`webapp/README.md`](webapp/README.md)

## Pipeline

`discover → tree-sitter extract → resolve (SCIP + heuristic) → Neo4j`

- **Nodes:** `File`, `Class`, `Function`, `Field`, `Annotation`. Each carries a stable
  `id = sha1(repo+kind+fqn)`, the shared label `:CodeNode`, and rich metadata: source range
  (incl. columns), `visibility`, `modifiers`, `is_static/abstract/async`, `return_type`,
  `param_count`, `docstring`, `signature`, `body_hash`, `extractor`, `last_indexed`. Python
  instance fields (`self.x`) are modeled as Field nodes.
- **Edges** (each tagged `confidence` + `origin` + evidence `file:line:col`):
  - structure — `CONTAINS`, `IMPORTS`
  - references — `CALLS`, `INSTANTIATES`, `EXTENDS`, `IMPLEMENTS`, `ANNOTATED_WITH`
  - types — `RETURNS`, `OF_TYPE`, `HAS_TYPE`, `HAS_GENERIC`
  - relationships — `OVERRIDES`, `READS`, `WRITES`, `THROWS`, `CATCHES`
- **Resolution (Stage 2), two tiers:**
  - **SCIP / Pyright** (precise, `EXTRACTED`) — Python `CALLS` + `OVERRIDES`. Measured
    84.8% precision / 92.5% recall vs the heuristic it replaced.
  - **Heuristic** (name + lexical scope, `INFERRED`/`AMBIGUOUS`) — fallback for Java and
    everything SCIP doesn't cover; type/state/exception edges resolve here too.
- **Confidence** = resolution certainty (`EXTRACTED`/`INFERRED`/`AMBIGUOUS`).
  **Origin** = how the fact entered (`EXTRACTED` from AST/index vs `DERIVED` by later analysis).

## Setup

```bash
cd graph_rag
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# Neo4j (Docker)
docker run -d --name cbrain-neo4j -p7474:7474 -p7687:7687 \
  -e NEO4J_AUTH=neo4j/testpassword \
  -e NEO4J_PLUGINS='["graph-data-science"]' neo4j:5

# scip-python (precise Python resolution; optional but recommended)
npm install --prefix scip_tooling @sourcegraph/scip-python@0.6.6
```

## Run

```bash
# index a repo
NEO4J_PASSWORD=testpassword ./.venv/bin/python -m graph_rag.cli index <path> --repo NAME
#   flags: --no-wipe (append)  --no-scip (heuristic only)

# web UI (add repo via github/zip, browse symbols/deps/source)
NEO4J_PASSWORD=testpassword ./.venv/bin/python -m uvicorn webapp.server:app --port 8000

# resolver coverage probe (no DB)
./.venv/bin/python measure_coverage.py <path> --repo NAME
```
Config via env: `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`,
`SCIP_PYTHON_BIN`. Browse Neo4j at http://localhost:7474.

## Example queries

```cypher
// blast radius — transitive callers (<=3 hops)
MATCH (caller)-[:CALLS*1..3]->(f:Function {name:'placeOrder'}) RETURN DISTINCT caller.fqn;

// state impact — who reads/writes a field
MATCH (fn)-[r:READS|WRITES]->(f:Field {repo:'primitive-pr', name:'balance'})
RETURN fn.fqn, type(r);

// where is a type used
MATCH (u)-[r:RETURNS|OF_TYPE|HAS_TYPE|HAS_GENERIC]->(t:Class {name:'Finding'})
RETURN u.fqn, type(r), r.evidence_file, r.evidence_line;

// overrides
MATCH (sub)-[:OVERRIDES]->(base) RETURN sub.fqn, base.fqn;

// edge trust/provenance
MATCH ()-[r:CALLS]->() RETURN r.confidence, r.origin, r.extractor, count(*);
```

## Layout

```
graph_rag/
  config.py        Neo4j connection + scip-python binary location
  ids.py           stable id + body_hash
  models.py        Node / Edge / RawRef / Confidence / Origin
  schema.py        label + edge-type allowlists (Cypher-injection guard)
  discovery.py     Stage 0 — walk + hash files
  languages.py     tree-sitter parser loading
  extractors/      Stage 1 — java.py, python.py, common.py (nodes + metadata + RawRefs)
  resolver.py      Stage 2 heuristic — name+scope resolution + coverage
  scip_resolver.py Stage 2 precise — SCIP/Pyright → EXTRACTED CALLS + OVERRIDES
  scip/            vendored SCIP protobuf (scip.proto + scip_pb2.py)
  store.py         Neo4j bootstrap + batched upserts (nodes + edge props)
  pipeline.py      orchestration
  cli.py           `index` command
  measure_coverage.py   dev probe (no DB)
  webapp/          deterministic browser UI (FastAPI + 1 static page)
```

## Status

Phase 1 (structural graph) in progress — done: Metadata & Provenance (M1), Type System (M2),
Symbol Resolution (M3, Python; scip-java blocked), Program Relationships (M4). Next: Static
Metrics (M5), Canonical IR (M6), Validation Suite (M7), then the semantic/retrieval/agent
phases. See [`STATUS.md`](STATUS.md) for the full roadmap.
