"""Neo4j writer: schema bootstrap + batched node/edge upserts.

Every node gets the shared label :CodeNode (single unique id index) plus its
specific label. Labels and rel-types are validated against the schema allowlist
before being interpolated into Cypher (Cypher can't parametrize them).
"""
from __future__ import annotations

import time
from collections import defaultdict

from neo4j import GraphDatabase

from .config import Neo4jConfig
from .models import Edge, Node
from .schema import SHARED_LABEL, assert_edge, assert_label

_BATCH = 1000


class GraphStore:
    def __init__(self, cfg: Neo4jConfig):
        self._cfg = cfg
        self._driver = GraphDatabase.driver(cfg.uri, auth=(cfg.user, cfg.password))

    def close(self):
        self._driver.close()

    def _run(self, query, **params):
        with self._driver.session(database=self._cfg.database) as s:
            return s.run(query, **params).consume()

    def bootstrap(self):
        self._run(
            f"CREATE CONSTRAINT code_node_id IF NOT EXISTS "
            f"FOR (n:{SHARED_LABEL}) REQUIRE n.id IS UNIQUE"
        )

    def wipe(self, repo: str):
        self._run(f"MATCH (n:{SHARED_LABEL} {{repo:$repo}}) DETACH DELETE n", repo=repo)

    def write_nodes(self, nodes: list[Node]):
        ts = int(time.time())
        by_label: dict[str, list[dict]] = defaultdict(list)
        for n in nodes:
            by_label[assert_label(n.label)].append({"id": n.id, "props": n.props()})
        for label, rows in by_label.items():
            q = (
                f"UNWIND $rows AS row "
                f"MERGE (n:{SHARED_LABEL} {{id: row.id}}) "
                f"SET n:{label}, n += row.props, n.last_indexed = $ts"
            )
            for i in range(0, len(rows), _BATCH):
                self._run(q, rows=rows[i:i + _BATCH], ts=ts)

    def write_edges(self, edges: list[Edge]):
        by_type: dict[str, list[dict]] = defaultdict(list)
        for e in edges:
            by_type[assert_edge(e.type)].append(
                {"src": e.src, "dst": e.dst, "props": e.props()}
            )
        for rtype, rows in by_type.items():
            q = (
                f"UNWIND $rows AS row "
                f"MATCH (a:{SHARED_LABEL} {{id: row.src}}) "
                f"MATCH (b:{SHARED_LABEL} {{id: row.dst}}) "
                f"MERGE (a)-[r:{rtype}]->(b) "
                f"SET r += row.props"
            )
            for i in range(0, len(rows), _BATCH):
                self._run(q, rows=rows[i:i + _BATCH])

    def counts(self, repo: str) -> dict:
        with self._driver.session(database=self._cfg.database) as s:
            nodes = s.run(
                f"MATCH (n:{SHARED_LABEL} {{repo:$repo}}) RETURN count(n) AS c",
                repo=repo,
            ).single()["c"]
            rels = s.run(
                f"MATCH (a:{SHARED_LABEL} {{repo:$repo}})-[r]->() RETURN count(r) AS c",
                repo=repo,
            ).single()["c"]
        return {"nodes": nodes, "relationships": rels}
