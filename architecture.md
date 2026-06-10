# CODEBASE KNOWLEDGE GRAPH - COMPLETE TECHNICAL REFERENCE

This document is the authoritative specification for the deterministic multiplex
code knowledge graph in this repository.

It is designed so an engineer or AI assistant can implement, debug, or extend
the system without additional context.

## 1) System Scope

The system performs deterministic static analysis over:
- Python FastAPI backend
- React/TypeScript frontend

It builds one multiplex property graph (multiple edge layers over shared nodes)
and persists it to:
- JSON (always)
- Neo4j (optional)

No LLM is used in graph construction.

Primary use-cases:
- Cross-file impact analysis
- Frontend-to-backend endpoint traceability
- Authorization auditing
- Concurrency surface inspection
- Documentation generation from graph layers

Core files:
- cpg/model.py
- cpg/backend.py
- cpg/frontend.py
- cpg/resolve.py
- cpg/store.py
- build.py

## 2) Ontology

### 2.1 Node types (9)

All nodes include at minimum:
- id
- kind
- name
- file
- line

Node kinds:
- File
- Class
- Function
- Route
- FrontendCall
- Table
- Lock
- ExternalModule
- ExternalSymbol

Neo4j labels:
- Every node must have :CodeNode plus one specific kind label.

Examples:
- (:CodeNode:Function {id:"func::..."})
- (:CodeNode:Route {id:"route::..."})

### 2.2 Relationship types (9)

Only these relationship types are valid:
- CONTAINS
- CALLS
- IMPORTS
- HANDLES
- CALLS_ENDPOINT
- READS_TABLE
- WRITES_TABLE
- GUARDED_BY
- ACQUIRES

No generic :REL fallback is allowed.

### 2.3 Stable ID grammar

- file::{relpath}
- class::{relpath}::{ClassName}
- func::{relpath}::{qualname}
- route::{METHOD}::{normalized_full_path}::{relpath}
- fecall::{relpath}::{line}::{METHOD}::{normalized_path}
- table::{name}
- lock::{relpath}::{name}
- ext::{dotted.module.name} (ExternalModule)
- ext::{name} (ExternalSymbol)

Important:
- Route ID must include relpath to prevent collisions.

### 2.4 Path normalization rules

- Collapse path parameters to {p}
- Strip protocol and host from absolute URLs
- Strip leading base URL variable prefix
- Strip query string
- Strip trailing slash (except root)
- Keep unresolved dynamic-only URLs as unresolved in frontend extraction

## 3) Functional Pipeline

### 3.1 build.py orchestration

- Parse CLI arguments
- Walk repository with deterministic skip list
- Extract backend payloads from Python files
- Extract frontend payloads from JS/TS files
- Resolve and unify graph
- Print counts by node kind and edge type
- Persist to JSON and optionally Neo4j

### 3.2 backend extraction (cpg/backend.py)

Emits nodes/edges for:
- Structural hierarchy (CONTAINS)
- Routes (HANDLES)
- Auth guard placeholders (GUARDED_BY placeholders keyed by handler)
- DB reads/writes (READS_TABLE/WRITES_TABLE)
- Concurrency lock usage (ACQUIRES)
- Imports (IMPORTS)
- Unresolved call sites (CALLS raw refs)

Key safeguards:
- dict.get("literal") is explicitly excluded from DB detection
- Auth dependencies are filtered by auth keywords
- include_router(prefix=...) calls are extracted for prefix chaining

### 3.3 frontend extraction (cpg/frontend.py)

Regex extraction for:
- axios-like method calls
- fetch() calls with optional method in options

Output:
- FrontendCall nodes
- CONTAINS edges from frontend file to call node

Normalization includes:
- ${...} -> {p}
- Strip host and query
- Strip leading base URL variable prefix

### 3.4 resolve pass (cpg/resolve.py)

Responsibilities:
- Merge per-file payloads into unified node/edge sets
- Resolve CALLS to internal functions
- Resolve GUARDED_BY placeholders to Function/ExternalSymbol
- Compute transitive include_router prefixes
- Re-key Route IDs and paths after full prefix accumulation
- Build FrontendCall -> Route bridge (CALLS_ENDPOINT)

Bridge behavior:
- Match on (method, normalized path)
- Add version-suffix alias for base URL stripped frontend calls
- Mark unmatched frontend calls with bridge_resolved=false

### 3.5 persistence (cpg/store.py)

JSON:
- Write nodes/edges as graph.json payload

Neo4j:
- MERGE nodes on :CodeNode{id}
- Apply specific node labels using fixed NODE_KINDS allowlist
- Write edges with fixed REL_TYPES allowlist and typed MERGE
- Warn on unknown node kinds or relationship types

## 4) Subgraphs

The multiplex graph is a union of these functional subgraphs.

### 4.1 Structure subgraph
- Nodes: File, Class, Function, FrontendCall
- Edges: CONTAINS

### 4.2 Dependency and call subgraph
- Nodes: Function, ExternalModule, ExternalSymbol, File
- Edges: CALLS, IMPORTS

### 4.3 Backend API subgraph
- Nodes: Route, Function, ExternalSymbol
- Edges: HANDLES, GUARDED_BY

### 4.4 Frontend-backend bridge subgraph
- Nodes: FrontendCall, Route
- Edges: CALLS_ENDPOINT

### 4.5 Data access subgraph
- Nodes: Function, Table
- Edges: READS_TABLE, WRITES_TABLE

### 4.6 Concurrency subgraph
- Nodes: Function, Lock
- Edges: ACQUIRES

### 4.7 Full multiplex view
- Nodes: File, Class, Function, Route, FrontendCall, Table, Lock,
  ExternalModule, ExternalSymbol
- Edges: CONTAINS, CALLS, IMPORTS, HANDLES, CALLS_ENDPOINT,
  READS_TABLE, WRITES_TABLE, GUARDED_BY, ACQUIRES

## 5) Known Failure Modes and Correct Fixes

### 5.1 All nodes only appear as :CodeNode
Cause:
- kind stored as property only; specific labels not applied

Fix:
- Apply labels in a per-kind static Cypher loop over fixed allowlist

### 5.2 Route collisions
Cause:
- route_id built from method+path only

Fix:
- Include relpath in route_id

### 5.3 Fake Table nodes from dict access
Cause:
- .get() classified as DB read

Fix:
- Early skip for dict.get("literal")
- Keep DB_READ_CALLS conservative

### 5.4 Non-auth deps as GUARDED_BY
Cause:
- Any Depends() treated as auth guard

Fix:
- Keyword-based auth dependency filter

### 5.5 Missing route prefixes
Cause:
- include_router chain not accumulated

Fix:
- Resolve include graph transitively and rewrite Route paths and IDs before bridging

## 6) Neo4j Model Rules

### 6.1 Labels vs properties

- n.kind = "Function" is a property
- :Function is a label
- They are not interchangeable

### 6.2 Cypher constraints

Cypher does not allow parameterized labels or relationship types.
Use static allowlist loops in Python and interpolate only controlled values.

### 6.3 Required constraint

CREATE CONSTRAINT cpg_id IF NOT EXISTS
FOR (n:CodeNode) REQUIRE n.id IS UNIQUE

Recommended indexes:
- One on each kind label id
- Route(method, path) composite index

## 7) Validation Checklist

### 7.1 Label health

- MATCH (n:Function) RETURN count(n)
- MATCH (n:Route) RETURN count(n)
- MATCH (n:File) RETURN count(n)

All should be > 0 for a populated graph.

### 7.2 Edge typing health

- MATCH ()-[r:REL]->() RETURN count(r)

Must be 0.

### 7.3 Route collision health

- MATCH (r:Route)-[:HANDLES]->(f:Function)
  WITH r, count(f) AS c
  WHERE c > 1
  RETURN r.id, c

Must return 0 rows.

### 7.4 Bridge health

- MATCH (:FrontendCall)-[e:CALLS_ENDPOINT]->(:Route)
  RETURN count(e)

Should be > 0 in repositories with frontend/backend overlap.

## 8) File Contracts

### cpg/model.py
- ID builders, normalize_path, node(), edge()

### cpg/backend.py
- extract_python(relpath, source, backend_root)
- Returns nodes, edges, imports, exports, module, relpath, router_includes

### cpg/frontend.py
- extract_frontend(relpath, source)
- Returns nodes, edges, relpath

### cpg/resolve.py
- build(per_file, fe_files)
- Returns fully resolved nodes, edges

### cpg/store.py
- to_json(nodes, edges, path)
- to_neo4j(nodes, edges, uri, user, password, database="neo4j", wipe=False)
- stats(nodes, edges)

### build.py
- CLI entrypoint and pipeline orchestration

## 9) Guardrails

Never:
- Write generic :REL edges
- Parameterize labels/types in Cypher
- Emit table nodes for dict keys
- Treat get_db/get_session as auth guards
- Generate route IDs without relpath
- Skip include_router prefix chaining before CALLS_ENDPOINT bridge
- Assume APOC is installed
