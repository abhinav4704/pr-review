"""Runtime configuration, read from environment with sane local defaults."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass


# npm shims the binary differently per OS: an extension-less script on
# POSIX, `scip-python.cmd` / `.ps1` on Windows. Probe `.cmd` before `.ps1`
# because the .cmd shim is what subprocess can launch directly on Windows.
_SCIP_BIN_NAMES = ("scip-python", "scip-python.cmd", "scip-python.ps1")


def scip_python_bin() -> str | None:
    """Locate the scip-python indexer binary.

    Order: $SCIP_PYTHON_BIN -> the copy installed under graph_rag/scip_tooling
    -> anything on PATH. Returns None if not found (caller falls back to the
    heuristic resolver).
    """
    env = os.environ.get("SCIP_PYTHON_BIN")
    if env:
        # Honor an explicit path, tolerating a missing Windows extension.
        for cand in (env, env + ".cmd", env + ".ps1"):
            if os.path.exists(cand):
                return cand
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # graph_rag/
    bindir = os.path.join(here, "scip_tooling", "node_modules", ".bin")
    for name in _SCIP_BIN_NAMES:
        local = os.path.join(bindir, name)
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
