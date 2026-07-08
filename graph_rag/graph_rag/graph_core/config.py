"""Runtime configuration, read from environment with sane local defaults.

Importing this module loads the repo-root `.env` (if present) so every other
module sees those values via `os.environ`. config is imported before llm/store
in the CLI, so this runs before any module-level env read.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

# graph_rag/graph_rag/graph_core/config.py -> repo root (where .env lives) is
# three dirs up: graph_core -> graph_rag (package) -> graph_rag (project root).
# NOTE: this was two dirs up before the graph_core/ package split — if this
# ever stops finding real env vars again, check this path first before
# assuming provider/credential env vars are simply unset.
_ENV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"
)
try:
    from dotenv import load_dotenv

    load_dotenv(_ENV_PATH)
except ImportError:  # dotenv optional; real env vars still work
    pass


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


# scip-java ships as a JVM app, not an npm package like scip-python — either a
# native launcher script from a GitHub release (sourcegraph/scip-java), or run
# on-demand via `coursier launch com.sourcegraph:scip-java_2.13:<version> --`.
# We don't vendor a copy (no npm-style local install path); the user installs
# it themselves (or coursier) and points at it, or puts it on PATH.
_SCIP_JAVA_BIN_NAMES = ("scip-java", "scip-java.bat", "scip-java.cmd")


def scip_java_bin() -> str | None:
    """Locate the scip-java indexer binary.

    Order: $SCIP_JAVA_BIN -> the copy installed under graph_rag/scip_tooling
    (if the user drops one there) -> anything on PATH. Returns None if not
    found (caller falls back to the heuristic resolver)."""
    env = os.environ.get("SCIP_JAVA_BIN")
    if env:
        for cand in (env, env + ".bat", env + ".cmd"):
            if os.path.exists(cand):
                return cand
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # graph_rag/
    bindir = os.path.join(here, "scip_tooling", "bin")
    for name in _SCIP_JAVA_BIN_NAMES:
        local = os.path.join(bindir, name)
        if os.path.exists(local):
            return local
    return shutil.which("scip-java")


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user: str = os.environ.get("NEO4J_USER", "neo4j")
    password: str = os.environ.get("NEO4J_PASSWORD", "testpassword")
    database: str = os.environ.get("NEO4J_DATABASE", "neo4j")


def neo4j_config() -> Neo4jConfig:
    return Neo4jConfig()
