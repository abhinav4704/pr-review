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

from .graph_contract import edge_relation
from .graph import CodeGraph

_EDGE_REL = {
    "calls": "CALLS",
    "imports": "IMPORTS",
    "references": "REFERENCES",
    "defines": "DEFINES",
    "inherits": "INHERITS",
    "overrides": "OVERRIDES",
    "decorates": "DECORATES",
    "instantiates": "INSTANTIATES",
    "implements": "IMPLEMENTS",
    "extends": "EXTENDS",
    "uses": "USES",
    "re_exports": "RE_EXPORTS",
}

DEFAULT_IMPACT_RELATIONS = [
    "CALLS",
    "INSTANTIATES",
    "OVERRIDES",
    "INHERITS",
    "IMPORTS",
    "REFERENCES",
    "USES",
    "IMPLEMENTS",
    "EXTENDS",
    "RE_EXPORTS",
    "DECORATES",
]


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
            etype = edge_relation(data, "calls")
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
            f"MATCH (caller:Node)-[:CALLS*1..{int(depth)}]->(n:Node {{id: $id}}) "
            "RETURN DISTINCT caller.id AS id",
            {"id": node_id},
        )
        return [r["id"] for r in rows]

    def reverse_dependents(self, node_id: str, depth: int = 2,
                           relations: Optional[List[str]] = None) -> List[Dict]:
        """General reverse dependency traversal for PR and repo analysis.

        Returns distinct dependent nodes that reach node_id via allowed relation
        types, up to depth hops.
        """
        if not self._available:
            return []
        rels = relations or DEFAULT_IMPACT_RELATIONS
        safe_rels = [r for r in rels if r in set(_EDGE_REL.values())]
        if not safe_rels:
            return []
        depth = max(1, min(int(depth), 6))
        rows = self._run(
            "MATCH p=(dep:Node)-[r*1.." + str(depth) + "]->(n:Node {id: $id}) "
            "WHERE ALL(rel IN r WHERE type(rel) IN $rels) "
            "RETURN DISTINCT dep.id AS id, dep.kind AS kind, dep.name AS name, "
            "dep.qualname AS qualname, dep.path AS path "
            "ORDER BY coalesce(dep.qualname, dep.path, dep.name)",
            {"id": node_id, "rels": safe_rels},
        )
        return [dict(r) for r in rows]

    def find_similar_nodes(self, name: str, kind: str, limit: int = 10) -> List[Dict]:
        if not self._available:
            return []
        rows = self._run(
            "MATCH (n:Node) WHERE n.name CONTAINS $name AND n.kind = $kind "
            "RETURN n.id AS id, n.path AS path, n.name AS name LIMIT $limit",
            {"name": name, "kind": kind, "limit": limit},
        )
        return [dict(r) for r in rows]

    # ── retrieval / exploration helpers ──────────────────────────────────────
    def list_kinds(self, pr_ref: str) -> List[str]:
        """Distinct node kinds present for a given graph (pr_ref scope)."""
        if not self._available:
            return []
        rows = self._run(
            "MATCH (n:Node {pr_ref: $ref}) "
            "RETURN DISTINCT n.kind AS kind ORDER BY kind",
            {"ref": pr_ref},
        )
        return [r["kind"] for r in rows]

    def nodes_by_kind(self, kind: str, pr_ref: str, limit: int = 2000) -> List[Dict]:
        """All nodes of one kind for the dropdown, ordered by qualname."""
        if not self._available:
            return []
        rows = self._run(
            "MATCH (n:Node {kind: $kind, pr_ref: $ref}) "
            "RETURN n.id AS id, n.name AS name, n.qualname AS qualname, "
            "n.path AS path "
            "ORDER BY coalesce(n.qualname, n.path, n.name) LIMIT $limit",
            {"kind": kind, "ref": pr_ref, "limit": limit},
        )
        return [dict(r) for r in rows]

    def get_node(self, node_id: str) -> Optional[Dict]:
        """Full property bag for a single node."""
        if not self._available:
            return None
        rows = self._run(
            "MATCH (n:Node {id: $id}) RETURN n{.*} AS n LIMIT 1",
            {"id": node_id},
        )
        return dict(rows[0]["n"]) if rows else None

    def neighbors(self, node_id: str, rel_type: str, direction: str,
                  depth: int = 1) -> List[Dict]:
        """Nodes connected to node_id via rel_type within `depth` hops.

        rel_type must be one of the known relationship names (validated against
        _EDGE_REL values, same safe-static-string approach as _upsert_edges).
        direction is "out" (node -> others) or "in" (others -> node).
        depth controls how many hops to traverse (1 = direct neighbours only).
        """
        if not self._available:
            return []
        allowed = set(_EDGE_REL.values())
        if rel_type not in allowed:
            raise ValueError(f"Unknown relationship type: {rel_type}")
        depth = max(1, min(int(depth), 5))  # clamp 1–5
        hops = f"*1..{depth}"
        if direction == "out":
            pattern = f"(n:Node {{id: $id}})-[:{rel_type}{hops}]->(m:Node)"
        elif direction == "in":
            pattern = f"(n:Node {{id: $id}})<-[:{rel_type}{hops}]-(m:Node)"
        else:
            raise ValueError(f"direction must be 'out' or 'in', got {direction!r}")
        rows = self._run(
            f"MATCH {pattern} WHERE m.id <> $id "
            "RETURN DISTINCT m.id AS id, m.kind AS kind, m.name AS name, "
            "m.qualname AS qualname, m.path AS path, "
            "m.start_line AS start_line, m.end_line AS end_line "
            "ORDER BY coalesce(m.qualname, m.path, m.name)",
            {"id": node_id},
        )
        return [dict(r) for r in rows]

    def nodes_at_lines(self, path: str, lines: List[int], pr_ref: str) -> List[Dict]:
        """Find the innermost graph node containing each changed line in a file.

        For each line number, picks the smallest-span node that contains it
        (e.g. a method rather than its parent class), then returns the unique set.
        """
        if not self._available or not lines:
            return []
        rows = self._run(
            "UNWIND $lines AS line "
            "MATCH (n:Node {path: $path, pr_ref: $ref}) "
            "WHERE n.start_line <= line AND n.end_line >= line AND n.kind <> 'file' "
            "WITH line, n ORDER BY (n.end_line - n.start_line) ASC "
            "WITH line, collect(n)[0] AS innermost "
            "WITH collect(DISTINCT innermost) AS nodes "
            "UNWIND nodes AS n "
            "RETURN n.id AS id, n.kind AS kind, n.name AS name, "
            "n.qualname AS qualname, n.path AS path, "
            "n.start_line AS start_line, n.end_line AS end_line "
            "ORDER BY n.start_line",
            {"lines": lines, "path": path, "ref": pr_ref},
        )
        return [dict(r) for r in rows]

    # ── architecture-digest helpers (all scoped by pr_ref) ───────────────────
    def kind_counts(self, pr_ref: str) -> Dict[str, int]:
        """Count of nodes per kind (files/functions/methods/classes/...)."""
        if not self._available:
            return {}
        rows = self._run(
            "MATCH (n:Node {pr_ref: $ref}) "
            "RETURN n.kind AS kind, count(*) AS c ORDER BY c DESC",
            {"ref": pr_ref},
        )
        return {r["kind"]: r["c"] for r in rows}

    @staticmethod
    def _module_of(path: str) -> str:
        """Top-level directory of a path, or '(root)' for top-level files."""
        if not path:
            return "(root)"
        parts = path.split("/")
        return parts[0] if len(parts) > 1 else "(root)"

    def module_edges(self, pr_ref: str) -> List[Dict]:
        """File->file IMPORTS aggregated to top-level directory (module) pairs."""
        if not self._available:
            return []
        rows = self._run(
            "MATCH (a:Node {pr_ref: $ref})-[:IMPORTS]->(b:Node {pr_ref: $ref}) "
            "RETURN a.path AS from_path, b.path AS to_path",
            {"ref": pr_ref},
        )
        weights: Dict[tuple, int] = {}
        for r in rows:
            fm = self._module_of(r["from_path"])
            tm = self._module_of(r["to_path"])
            if fm == tm:
                continue  # intra-module imports don't show coupling
            weights[(fm, tm)] = weights.get((fm, tm), 0) + 1
        return [
            {"from_module": fm, "to_module": tm, "weight": w}
            for (fm, tm), w in sorted(weights.items(), key=lambda kv: -kv[1])
        ]

    def module_sizes(self, pr_ref: str) -> List[Dict]:
        """Node count per top-level directory (module footprint)."""
        if not self._available:
            return []
        rows = self._run(
            "MATCH (n:Node {pr_ref: $ref}) WHERE n.kind <> 'file' "
            "RETURN n.path AS path",
            {"ref": pr_ref},
        )
        sizes: Dict[str, int] = {}
        for r in rows:
            m = self._module_of(r["path"])
            sizes[m] = sizes.get(m, 0) + 1
        return [
            {"module": m, "nodes": c}
            for m, c in sorted(sizes.items(), key=lambda kv: -kv[1])
        ]

    def top_fan_in(self, pr_ref: str, limit: int = 20) -> List[Dict]:
        """Functions/methods ranked by number of distinct callers (hotspots)."""
        if not self._available:
            return []
        rows = self._run(
            "MATCH (m:Node {pr_ref: $ref})<-[:CALLS]-(c:Node) "
            "WITH m, count(DISTINCT c) AS fan WHERE fan > 0 "
            "RETURN m.qualname AS qualname, m.name AS name, m.path AS path, "
            "m.kind AS kind, fan ORDER BY fan DESC LIMIT $limit",
            {"ref": pr_ref, "limit": limit},
        )
        return [dict(r) for r in rows]

    def god_classes(self, pr_ref: str, limit: int = 15) -> List[Dict]:
        """Classes ranked by method count (DEFINES-out to methods)."""
        if not self._available:
            return []
        rows = self._run(
            "MATCH (c:Node {pr_ref: $ref, kind: 'class'})-[:DEFINES]->(m:Node) "
            "WHERE m.kind = 'method' "
            "WITH c, count(m) AS methods "
            "RETURN c.qualname AS qualname, c.name AS name, c.path AS path, "
            "methods ORDER BY methods DESC LIMIT $limit",
            {"ref": pr_ref, "limit": limit},
        )
        return [dict(r) for r in rows]

    def top_subclassed(self, pr_ref: str, limit: int = 15) -> List[Dict]:
        """Classes ranked by number of subclasses (incoming INHERITS)."""
        if not self._available:
            return []
        rows = self._run(
            "MATCH (parent:Node {pr_ref: $ref, kind: 'class'})<-[:INHERITS]-(child:Node) "
            "WITH parent, count(DISTINCT child) AS subs "
            "RETURN parent.qualname AS qualname, parent.name AS name, "
            "parent.path AS path, subs ORDER BY subs DESC LIMIT $limit",
            {"ref": pr_ref, "limit": limit},
        )
        return [dict(r) for r in rows]

    def entrypoints(self, pr_ref: str, limit: int = 100) -> List[Dict]:
        """HTTP routes and events/tasks — the repo's entry points."""
        if not self._available:
            return []
        rows = self._run(
            "MATCH (n:Node {pr_ref: $ref}) WHERE n.kind IN ['route', 'event'] "
            "RETURN n.kind AS kind, n.qualname AS qualname, n.name AS name, "
            "n.path AS path ORDER BY n.kind, n.path LIMIT $limit",
            {"ref": pr_ref, "limit": limit},
        )
        return [dict(r) for r in rows]

    def orphans(self, pr_ref: str, limit: int = 40) -> List[Dict]:
        """Functions/methods with no callers and not entrypoints/tests — dead code."""
        if not self._available:
            return []
        rows = self._run(
            "MATCH (n:Node {pr_ref: $ref}) "
            "WHERE n.kind IN ['function', 'method'] AND NOT n.is_test "
            "AND NOT (n)<-[:CALLS]-() AND NOT (n)<-[:OVERRIDES]-() "
            "RETURN n.qualname AS qualname, n.name AS name, n.path AS path, "
            "n.kind AS kind ORDER BY n.path LIMIT $limit",
            {"ref": pr_ref, "limit": limit},
        )
        return [dict(r) for r in rows]

    def close(self) -> None:
        if self._driver:
            self._driver.close()
