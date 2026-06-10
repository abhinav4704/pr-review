"""Persist the graph: JSON (always) and Neo4j (if driver + creds available)."""
from __future__ import annotations
import json


def to_json(nodes, edges, path: str, *, source_map: dict | None = None):
    payload: dict = {"nodes": nodes, "edges": edges}
    if source_map is not None:
        payload["source_map"] = source_map
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)


def stats(nodes, edges) -> dict:
    by_node: dict[str, int] = {}
    for n in nodes:
        by_node[n["kind"]] = by_node.get(n["kind"], 0) + 1
    by_edge: dict[str, int] = {}
    for e in edges:
        by_edge[e["type"]] = by_edge.get(e["type"], 0) + 1
    return {"nodes_total": len(nodes), "edges_total": len(edges),
            "by_node": by_node, "by_edge": by_edge}


# Reserved keys that are stored as the relationship type / endpoints, not props.
_EDGE_META = {"src", "dst", "type"}
_NODE_META = {"id", "kind"}


def to_neo4j(nodes, edges, uri: str, user: str, password: str,
             database: str = "neo4j", wipe: bool = False):
    from neo4j import GraphDatabase  # imported lazily so JSON path needs no dep
    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session(database=database) as s:
        if wipe:
            s.run("MATCH (n) DETACH DELETE n")
        NODE_KINDS = ["File", "Class", "Function", "Route", "FrontendCall",
                      "Table", "Lock", "ExternalModule", "ExternalSymbol",
                      "Decorator", "Component"]
        # constraints for stable MERGE (one per concrete node kind)
        for kind in NODE_KINDS:
            s.run(
                f"CREATE CONSTRAINT cpg_id_{kind.lower()} IF NOT EXISTS "
                f"FOR (n:{kind}) REQUIRE n.id IS UNIQUE"
            )
        # upsert nodes on concrete kind labels only (no shared :CodeNode label)
        for kind in NODE_KINDS:
            rows = [n for n in nodes if n.get("kind") == kind]
            if not rows:
                continue
            for batch in _chunks(rows, 1000):
                s.run(
                    f"UNWIND $rows AS row "
                    f"MERGE (n:{kind} {{id: row.id}}) "
                    f"SET n += row.props, n.kind = row.kind",
                    rows=[{"id": n["id"], "kind": n["kind"],
                           "props": {k: v for k, v in n.items() if k not in _NODE_META
                                     and not isinstance(v, (list, dict))}}
                          for n in batch],
                )
        unknown_kinds = {n.get("kind", "") for n in nodes if n.get("kind", "") not in NODE_KINDS}
        if unknown_kinds:
            import sys
            print(f"WARNING: unknown node kinds not labeled: {unknown_kinds}",
                  file=sys.stderr)
        # upsert edges — each relationship type gets its own statically-typed MERGE
        # so Neo4j stores real typed relationships, not a generic :REL.
        # The type names are from a fixed allowlist and never derived from user data.
        REL_TYPES = ["CONTAINS", "CALLS", "IMPORTS", "HANDLES", "CALLS_ENDPOINT",
                     "READS_TABLE", "WRITES_TABLE", "GUARDED_BY", "ACQUIRES",
                     "DECORATES", "DEPENDS_ON", "VALIDATES_WITH",
                     "INHERITS", "RAISES", "RENDERS", "USES_COMPONENT"]
        unknown = {e["type"] for e in edges if e["type"] not in REL_TYPES}
        if unknown:
            import sys
            print(f"WARNING: unknown relationship types skipped: {unknown}",
                  file=sys.stderr)
        for rtype in REL_TYPES:
            rows = [e for e in edges if e["type"] == rtype]
            if not rows:
                continue
            for batch in _chunks(rows, 1000):
                s.run(
                    f"UNWIND $rows AS row "
                    f"MATCH (a {{id: row.src}}) "
                    f"MATCH (b {{id: row.dst}}) "
                    f"MERGE (a)-[r:{rtype}]->(b) "
                    f"SET r += row.props",
                    rows=[{"src": e["src"], "dst": e["dst"],
                           "props": {k: v for k, v in e.items()
                                     if k not in _EDGE_META
                                     and not isinstance(v, (list, dict))}}
                          for e in batch],
                )
    driver.close()


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]