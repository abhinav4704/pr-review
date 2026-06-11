"""Optional Neo4j graph persistence.

If NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD are set (env or passed explicitly),
the code graph is persisted to Neo4j and queries use Cypher.

If Neo4j is not configured, this module is a no-op and the pipeline uses the
in-memory NetworkX graph unchanged.

Usage:
    from pr_review.neo4j_store import Neo4jStore
    store = Neo4jStore.from_env()          # None if not configured
    if store:
        store.push(cg)                      # persist graph to Neo4j
        callers = store.callers(node_id)    # fast Cypher query
        store.close()

The graph schema:
    (:Node {id, kind, path, name, qualname, start_line, end_line, lang, is_test})
    [:CALLS | :IMPORTS | :DEFINES | :INHERITS | :OVERRIDES | :DECORATES | :INSTANTIATES]
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .graph import CodeGraph

_EDGE_REL = {
    "calls": "CALLS",
    "imports": "IMPORTS",
    "defines": "DEFINES",
    "inherits": "INHERITS",
    "overrides": "OVERRIDES",
    "decorates": "DECORATES",
    "instantiates": "INSTANTIATES",
}


class Neo4jStore:
    def __init__(self, uri: str, user: str, password: str,
                 database: str = "neo4j") -> None:
        try:
            from neo4j import GraphDatabase
            self._driver = GraphDatabase.driver(uri, auth=(user, password))
            self._db = database
            self._available = True
        except ImportError:
            self._available = False
            self._driver = None
            self._db = database

    @classmethod
    def from_env(cls,
                 uri: Optional[str] = None,
                 user: Optional[str] = None,
                 password: Optional[str] = None) -> Optional["Neo4jStore"]:
        uri = uri or os.environ.get("NEO4J_URI", "")
        user = user or os.environ.get("NEO4J_USER", "neo4j")
        password = password or os.environ.get("NEO4J_PASSWORD", "")
        if not (uri and password):
            return None
        store = cls(uri, user, password)
        if not store._available:
            return None
        return store

    def _run(self, cypher: str, params: Dict[str, Any] | None = None) -> List[Any]:
        if not self._available or not self._driver:
            return []
        with self._driver.session(database=self._db) as session:
            result = session.run(cypher, params or {})
            return [record for record in result]

    def push(self, cg: CodeGraph, pr_ref: str = "HEAD") -> None:
        """Upsert the full graph into Neo4j. Safe to call multiple times."""
        if not self._available:
            return

        # nodes
        nodes_batch = []
        for nid, data in cg.g.nodes(data=True):
            nodes_batch.append({
                "id": nid,
                "kind": data.get("kind", "unknown"),
                "path": data.get("path", ""),
                "name": data.get("name", ""),
                "qualname": data.get("qualname", ""),
                "start_line": data.get("start_line", 0),
                "end_line": data.get("end_line", 0),
                "lang": data.get("lang", ""),
                "is_test": bool(data.get("is_test", False)),
                "pr_ref": pr_ref,
            })
            if len(nodes_batch) >= 500:
                self._upsert_nodes(nodes_batch)
                nodes_batch = []
        if nodes_batch:
            self._upsert_nodes(nodes_batch)

        # edges grouped by type
        edges_by_type: Dict[str, List[Dict]] = {}
        for u, v, data in cg.g.edges(data=True):
            etype = data.get("type", "calls")
            rel = _EDGE_REL.get(etype, "CALLS")
            edges_by_type.setdefault(rel, []).append({"from": u, "to": v})

        for rel, batch in edges_by_type.items():
            for i in range(0, len(batch), 500):
                self._upsert_edges(rel, batch[i:i+500])

    def _upsert_nodes(self, batch: List[Dict]) -> None:
        self._run(
            "UNWIND $batch AS n "
            "MERGE (node:Node {id: n.id}) "
            "SET node += {kind: n.kind, path: n.path, name: n.name, "
            "qualname: n.qualname, start_line: n.start_line, end_line: n.end_line, "
            "lang: n.lang, is_test: n.is_test, pr_ref: n.pr_ref}",
            {"batch": batch},
        )

    def _upsert_edges(self, rel_type: str, batch: List[Dict]) -> None:
        # rel_type is a safe static string from _EDGE_REL — no injection risk
        self._run(
            f"UNWIND $batch AS e "
            f"MATCH (a:Node {{id: e.from}}), (b:Node {{id: e.to}}) "
            f"MERGE (a)-[:{rel_type}]->(b)",
            {"batch": batch},
        )

    def callers(self, node_id: str, depth: int = 2) -> List[str]:
        if not self._available:
            return []
        rows = self._run(
            "MATCH (caller:Node)-[:CALLS*1..{d}]->(n:Node {{id: $id}}) "
            "RETURN DISTINCT caller.id AS id".replace("{d}", str(depth)),
            {"id": node_id},
        )
        return [r["id"] for r in rows]

    def find_similar_nodes(self, name: str, kind: str, limit: int = 10) -> List[Dict]:
        if not self._available:
            return []
        rows = self._run(
            "MATCH (n:Node) WHERE n.name CONTAINS $name AND n.kind = $kind "
            "RETURN n.id AS id, n.path AS path, n.name AS name LIMIT $limit",
            {"name": name, "kind": kind, "limit": limit},
        )
        return [dict(r) for r in rows]

    def close(self) -> None:
        if self._driver:
            self._driver.close()
