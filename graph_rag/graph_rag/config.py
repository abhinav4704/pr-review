"""Runtime configuration, read from environment with sane local defaults."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass


def scip_python_bin() -> str | None:
    """Locate the scip-python indexer binary.

    Order: $SCIP_PYTHON_BIN -> the copy installed under graph_rag/scip_tooling
    -> anything on PATH. Returns None if not found (caller falls back to the
    heuristic resolver).
    """
    env = os.environ.get("SCIP_PYTHON_BIN")
    if env and os.path.exists(env):
        return env
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # graph_rag/
    local = os.path.join(here, "scip_tooling", "node_modules", ".bin", "scip-python")
    if os.path.exists(local):
        return local
    return shutil.which("scip-python")


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user: str = os.environ.get("NEO4J_USER", "neo4j")
    password: str = os.environ.get("NEO4J_PASSWORD", "testpassword")
    database: str = os.environ.get("NEO4J_DATABASE", "neo4j")


def neo4j_config() -> Neo4jConfig:
    return Neo4jConfig()
