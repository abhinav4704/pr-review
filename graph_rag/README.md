# graph_rag — Codebase Brain graph builder (Phase 0)

Builds the Neo4j knowledge graph from source via tree-sitter.
Design: see [`../ARCHITECTURE.md`](../ARCHITECTURE.md).

## What Phase 0 does

`discover → tree-sitter extract (Java + Python) → heuristic name-resolution → Neo4j`

- **Nodes:** `File`, `Class` (class/interface/enum/record), `Function`
  (method/constructor/function), `Field`, `Annotation`. Each carries a stable
  `id = hash(repo+kind+fqn)`, plus the shared label `:CodeNode`.
- **Edges:** `CONTAINS`, `IMPORTS`, `CALLS`, `INSTANTIATES`, `EXTENDS`,
  `IMPLEMENTS`, `ANNOTATED_WITH`, each tagged `confidence`
  (`EXTRACTED|INFERRED|AMBIGUOUS`).
- **Resolution coverage metric** is printed per run — the Phase-0 health signal.

> Resolution is currently **heuristic name-matching** (no type info), so `CALLS`
> coverage is intentionally low. Stage 2 precision comes from scip-java / Pyright
> (next step).

## Setup

```bash
cd graph_rag
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# Neo4j (Docker)
docker run -d --name cbrain-neo4j -p7474:7474 -p7687:7687 \
  -e NEO4J_AUTH=neo4j/testpassword \
  -e NEO4J_PLUGINS='["graph-data-science"]' neo4j:5
```

## Run

```bash
NEO4J_PASSWORD=testpassword ./.venv/bin/python -m graph_rag.cli index <path> [--repo NAME] [--no-wipe]
```

Examples:

```bash
./.venv/bin/python -m graph_rag.cli index samples --repo demo-java
./.venv/bin/python -m graph_rag.cli index ../primitive-pr/pr_review --repo primitive-pr
```

Config via env: `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`.
Browse the graph at http://localhost:7474.

## Example queries

```cypher
// containment
MATCH (c:Class {name:'OrderService'})-[:CONTAINS]->(m:Function) RETURN m.signature;

// blast radius — transitive callers (<=3 hops)
MATCH (caller)-[:CALLS*1..3]->(b:Function {name:'placeOrder'}) RETURN DISTINCT caller.fqn;

// class hierarchy + annotations
MATCH (c:Class)-[e:EXTENDS|IMPLEMENTS|ANNOTATED_WITH]->(t) RETURN c.name, type(e), t.name;
```

## Layout

```
graph_rag/
  config.py       Neo4j connection (env-driven)
  ids.py          stable id + body_hash
  models.py       Node / Edge / RawRef / Confidence
  schema.py       label + edge-type allowlists (Cypher-injection guard)
  discovery.py    Stage 0 — walk + hash files
  languages.py    tree-sitter parser loading
  extractors/     Stage 1 — java.py, python.py, common.py
  resolver.py     Stage 2 — heuristic name resolution + coverage
  store.py        Neo4j bootstrap + batched upserts
  pipeline.py     orchestration
  cli.py          `index` command
```

## Next (Phase 1+)

scip-java / Pyright precise resolution · semantic summaries + embeddings ·
vector index · communities (GDS Leiden) · export artifact · git-hook incremental.
