"""Node/edge model for the deterministic multiplex code knowledge graph.

A node is a dict: {"id", "kind", "name", "file", "line", ...attrs}
An edge is a dict: {"src", "dst", "type", ...attrs}

IDs are stable and content-addressable so re-runs MERGE cleanly into Neo4j.
"""
from __future__ import annotations
import re


# ---- ID builders -----------------------------------------------------------

def file_id(relpath: str) -> str:
    return f"file::{relpath}"


def class_id(relpath: str, name: str) -> str:
    return f"class::{relpath}::{name}"


def func_id(relpath: str, qualname: str) -> str:
    # qualname includes the owning class for methods, e.g. "RecordService.create"
    return f"func::{relpath}::{qualname}"


def route_id(method: str, path: str, relpath: str = "") -> str:
    return f"route::{method.upper()}::{normalize_path(path)}::{relpath}"


def fe_call_id(relpath: str, line: int, method: str, path: str) -> str:
    return f"fecall::{relpath}::{line}::{method.upper()}::{normalize_path(path)}"


def table_id(name: str) -> str:
    return f"table::{name}"


def lock_id(relpath: str, name: str) -> str:
    return f"lock::{relpath}::{name}"


def external_id(dotted: str) -> str:
    return f"ext::{dotted}"


def decorator_id(relpath: str, name: str, target_id: str) -> str:
    """Unique per (file, decorator-name, decorated-target) triple."""
    return f"dec::{relpath}::{name}::{target_id}"


def component_id(relpath: str, name: str) -> str:
    return f"comp::{relpath}::{name}"


# ---- helpers ---------------------------------------------------------------

_PARAM_BRACE = re.compile(r"\{[^/}]*\}")


def normalize_path(path: str) -> str:
    """Normalise a URL path so frontend and backend forms match.

    /records/{id}     -> /records/{p}
    /records/${recId} -> /records/{p}   (frontend, handled before this)
    trailing slash removed.
    """
    p = path.strip()
    # collapse any path parameter token to a single placeholder
    p = _PARAM_BRACE.sub("{p}", p)
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def node(id_: str, kind: str, name: str, file: str = "", line: int = 0, **attrs) -> dict:
    # line_start mirrors line; line_end may be passed explicitly via attrs.
    line_end = attrs.pop("line_end", line)
    d = {"id": id_, "kind": kind, "name": name, "file": file,
         "line": line, "line_start": line, "line_end": line_end}
    d.update(attrs)
    return d


def edge(src: str, dst: str, type_: str, **attrs) -> dict:
    d = {"src": src, "dst": dst, "type": type_}
    d.update(attrs)
    return d