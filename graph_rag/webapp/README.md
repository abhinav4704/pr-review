# Codebase Brain — deterministic web browser

A minimal UI over the graph. **No LLM** — everything is read from Neo4j + source on disk.

- **Add a repo** — GitHub URL (shallow clone) or `.zip` upload → runs the indexing pipeline.
- **Search** functions / classes (also files / fields).
- **Node view** — metadata, dependencies (outgoing `→` and incoming `← used by`, each with
  confidence + provenance `file:line` + extractor), and the real **source code** (sliced by
  the node's line range).

## Run
```
# Neo4j must be up (docker start cbrain-neo4j) and the repo already buildable.
cd graph_rag
NEO4J_PASSWORD=testpassword ./.venv/bin/python -m uvicorn webapp.server:app --port 8000
# open http://localhost:8000
```

## Notes
- Repos are tracked in `webapp/repos/registry.json` (name → on-disk root, for source viewing).
  `primitive-pr` is auto-seeded if `../primitive-pr` exists.
- Cloned/uploaded repos land in `webapp/repos/<name>` (gitignored).
- Re-adding a repo re-indexes it (wipes that repo's nodes first; other repos are untouched).
- API: `GET /api/repos`, `POST /api/repos/github`, `POST /api/repos/upload`,
  `GET /api/nodes?repo=&query=&label=`, `GET /api/nodes/{id}`.
