"""Stage 2 (precise) — resolve CALLS from a SCIP index instead of name heuristics.

We run a language-specific SCIP indexer over the repo (scip-python, Pyright-
backed, for Python; scip-java, Maven/Gradle-build-backed, for Java), which
emits a SCIP index: for every identifier *occurrence* it records the global
symbol it refers to and whether that occurrence is a definition / read /
write / import. That gives us type-precise, cross-file resolution that the
name-matching heuristic can only guess at (measured for Python: the heuristic
was ~85% precise / ~93% recall vs SCIP).

How we map SCIP symbols onto our graph, robustly and without parsing SCIP's
descriptor sigils, is identical for both languages (`resolve_edges` below is
language-agnostic, only the indexer invocation differs):

  1. A SCIP symbol's *definition occurrence* gives `(file, line)`. Our nodes
     already carry `(file, start_line)`. Match on location  ->  `symbol -> node`.
  2. A non-definition occurrence of a function symbol is a **call**. Its line,
     placed against our Function line-ranges, gives the enclosing call site.
     Emit `CALLS(call_site -> target)` at EXTRACTED confidence.

Only CALLS + OVERRIDES are taken over here for now; the heuristic still owns
EXTENDS / IMPLEMENTS / INSTANTIATES / ANNOTATED_WITH / IMPORTS / AUTOWIRED for
both languages. (READS/WRITES are a natural extension — SCIP tags reads, but
neither indexer build here emits WriteAccess, and we don't yet model instance
fields as nodes for Python, so that waits.)

scip-java specifically needs a working Maven/Gradle build (it compiles the
project with a semanticdb-javac plugin attached to get type info), so it is
only attempted when a build file (pom.xml / build.gradle[.kts]) is present at
the repo root, and any failure (missing binary, no JDK, build failure, no
network to fetch deps) degrades gracefully to the heuristic resolver, exactly
like the scip-python path.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass

from .config import scip_java_bin, scip_python_bin
from .models import Confidence, Edge, Node, Origin

# SCIP SymbolRole bitmask (from scip.proto)
ROLE_DEFINITION = 0x1
ROLE_IMPORT = 0x2
ROLE_WRITE = 0x4
ROLE_READ = 0x8


@dataclass
class ScipReport:
    available: bool = False           # was a SCIP index produced/parsed?
    tool: str = ""                    # indexer name+version
    documents: int = 0
    inrepo_defs: int = 0              # in-repo symbols with a definition
    mapped_defs: int = 0             # ... that matched one of our nodes
    calls: int = 0                    # distinct CALLS edges emitted
    overrides: int = 0                # distinct OVERRIDES edges emitted


def run_scip_python(repo_root: str, project_name: str, out_path: str) -> str | None:
    """Run scip-python with cwd=repo_root (it discovers files relative to cwd).
    Returns the index path on success, else None."""
    binpath = scip_python_bin()
    if not binpath:
        return None
    try:
        subprocess.run(
            # --project-version is required: without it scip-python defaults the
            # version to `git rev-parse` and crashes on a non-git directory
            # (e.g. a vendored/extracted source tree). The value is not used in
            # our definition-location mapping, so a static placeholder is fine.
            [binpath, "index", "--project-name", project_name,
             "--project-version", "0.0.0",
             "--output", os.path.abspath(out_path), "--quiet"],
            cwd=os.path.abspath(repo_root),
            check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return out_path if os.path.exists(out_path) else None


_JAVA_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")


def has_java_build_tool(repo_root: str) -> str | None:
    """Detect a Maven/Gradle build at the repo root (multi-module repos keep
    their root pom.xml/build.gradle here too). Returns "maven"/"gradle", or
    None if neither is present. scip-java needs one to resolve dependencies
    and compile with its semanticdb-javac plugin, so we skip attempting it
    entirely rather than shelling out to a doomed indexer run."""
    if os.path.exists(os.path.join(repo_root, "pom.xml")):
        return "maven"
    if os.path.exists(os.path.join(repo_root, "build.gradle")) or os.path.exists(
        os.path.join(repo_root, "build.gradle.kts")
    ):
        return "gradle"
    return None


def run_scip_java(repo_root: str, out_path: str, build_tool: str) -> str | None:
    """Run scip-java with cwd=repo_root. Requires a working Maven/Gradle build
    (it compiles the project via a semanticdb-javac plugin to get type info),
    so this is only worth calling when `has_java_build_tool` found one.
    Returns the index path on success, else None (caller falls back to the
    heuristic resolver) - any subprocess/tooling failure here (missing JDK,
    dependency resolution needing network access, compile errors) is expected
    to be common in constrained/offline environments and is not fatal."""
    binpath = scip_java_bin()
    if not binpath:
        return None
    try:
        subprocess.run(
            [binpath, "index", "--build-tool", build_tool,
             "--output", os.path.abspath(out_path)],
            cwd=os.path.abspath(repo_root),
            check=True, capture_output=True, text=True, timeout=900,
        )
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return None
    return out_path if os.path.exists(out_path) else None


def _is_inrepo(symbol: str, project_name: str) -> bool:
    """SCIP symbol = `<scheme> <manager> <pkg> <version> <descriptor>`; in-repo
    symbols carry the project package. `local N` symbols are intra-function."""
    if symbol.startswith("local "):
        return False
    parts = symbol.split(" ", 4)
    return len(parts) >= 5 and parts[2] == project_name


def resolve_edges(nodes: list[Node], index_path: str, project_name: str,
                   tool_name: str = "scip"):
    """Parse a SCIP index -> (EXTRACTED edges [CALLS + OVERRIDES], ScipReport).
    Language-agnostic: works the same for a scip-python or scip-java index,
    since both emit the same SCIP protobuf schema. `tool_name` only tags the
    emitted edges' `extractor` field for provenance."""
    from .scip import scip_pb2  # lazy: requires the `protobuf` runtime

    index = scip_pb2.Index()
    with open(index_path, "rb") as fh:
        index.ParseFromString(fh.read())

    report = ScipReport(
        available=True,
        tool=f"{index.metadata.tool_info.name} {index.metadata.tool_info.version}".strip(),
        documents=len(index.documents),
    )

    # Our nodes, indexed for location lookup and enclosing-function lookup.
    node_by_loc: dict[tuple[str, int], Node] = {}
    spans: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for n in nodes:
        if n.label in ("Function", "Class"):
            node_by_loc[(n.file, n.start_line)] = n
        if n.label == "Function":
            spans[n.file].append((n.start_line, n.end_line, n.id))
    for v in spans.values():
        v.sort()

    def enclosing_function(relpath: str, line1: int) -> str | None:
        found = None
        for st, en, nid in spans.get(relpath, []):
            if st <= line1 <= en:
                found = nid  # sorted by start -> last match is innermost
            elif st > line1:
                break
        return found

    # 1. symbol -> node, via each in-repo symbol's definition occurrence.
    sym_to_node: dict[str, Node] = {}
    for doc in index.documents:
        for occ in doc.occurrences:
            if occ.symbol_roles & ROLE_DEFINITION and _is_inrepo(occ.symbol, project_name):
                if occ.symbol in sym_to_node:
                    continue
                report.inrepo_defs += 1
                node = node_by_loc.get((doc.relative_path, occ.range[0] + 1))
                if node is not None:
                    sym_to_node[occ.symbol] = node
                    report.mapped_defs += 1

    # 2. non-definition occurrences of a function symbol = calls. Keep one
    #    representative evidence location (file:line:col) per distinct edge.
    calls: dict[tuple[str, str], tuple[str, int, int]] = {}
    for doc in index.documents:
        for occ in doc.occurrences:
            if occ.symbol_roles & ROLE_DEFINITION:
                continue
            target = sym_to_node.get(occ.symbol)
            if target is None or target.label != "Function":
                continue
            src = enclosing_function(doc.relative_path, occ.range[0] + 1)
            if src and src != target.id:
                calls.setdefault(
                    (src, target.id),
                    (doc.relative_path, occ.range[0] + 1, occ.range[1]),
                )

    edges = [
        Edge("CALLS", s, d, Confidence.EXTRACTED.value,
             origin=Origin.EXTRACTED.value, extractor=tool_name,
             evidence_file=ev[0], evidence_line=ev[1], evidence_col=ev[2])
        for (s, d), ev in calls.items()
    ]
    report.calls = len(edges)

    # 3. OVERRIDES — a method's SymbolInformation lists the base methods it
    #    implements/overrides (Relationship.is_implementation). Both must be in-repo.
    overrides: dict[tuple[str, str], Node] = {}
    for doc in index.documents:
        for si in doc.symbols:
            src_node = sym_to_node.get(si.symbol)
            if src_node is None or src_node.label != "Function":
                continue
            for rel in si.relationships:
                if not rel.is_implementation:
                    continue
                tgt = sym_to_node.get(rel.symbol)
                if tgt is not None and tgt.label == "Function" and tgt.id != src_node.id:
                    overrides.setdefault((src_node.id, tgt.id), src_node)
    for (s, d), sn in overrides.items():
        edges.append(Edge(
            "OVERRIDES", s, d, Confidence.EXTRACTED.value,
            origin=Origin.EXTRACTED.value, extractor=tool_name,
            evidence_file=sn.file, evidence_line=sn.start_line, evidence_col=sn.start_col))
    report.overrides = len(overrides)
    return edges, report


def scip_resolve(nodes: list[Node], repo_root: str, project_name: str,
                 index_path: str | None = None):
    """High-level entry: ensure a SCIP index exists, then resolve CALLS.

    If `index_path` is given it is reused; otherwise scip-python is run into a
    temp file. Returns (edges, ScipReport). On any failure returns ([], report
    with available=False) so the pipeline can fall back to the heuristic.
    """
    tmp = None
    try:
        if index_path is None:
            tmp = tempfile.NamedTemporaryFile(suffix=".scip", delete=False)
            tmp.close()
            index_path = run_scip_python(repo_root, project_name, tmp.name)
            if index_path is None:
                return [], ScipReport(available=False)
        return resolve_edges(nodes, index_path, project_name, tool_name="scip-python")
    finally:
        if tmp is not None and os.path.exists(tmp.name):
            os.unlink(tmp.name)


def scip_resolve_java(nodes: list[Node], repo_root: str, project_name: str,
                      index_path: str | None = None):
    """Same as `scip_resolve` but for Java via scip-java. Returns ([], report
    with available=False) immediately (no subprocess attempt) if no Maven/
    Gradle build file is present, since scip-java cannot resolve dependencies
    or compile without one."""
    tmp = None
    try:
        if index_path is None:
            build_tool = has_java_build_tool(repo_root)
            if build_tool is None:
                return [], ScipReport(available=False)
            tmp = tempfile.NamedTemporaryFile(suffix=".scip", delete=False)
            tmp.close()
            index_path = run_scip_java(repo_root, tmp.name, build_tool)
            if index_path is None:
                return [], ScipReport(available=False)
        return resolve_edges(nodes, index_path, project_name, tool_name="scip-java")
    finally:
        if tmp is not None and os.path.exists(tmp.name):
            os.unlink(tmp.name)
