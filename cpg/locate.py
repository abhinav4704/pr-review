"""Locate graph seed nodes from a raw code snippet (or changed line ranges).

The PR-review retrieval pipeline never asks the caller for a node id.  Given a
raw snippet, :func:`seeds_from_snippet` parses it, pulls out the identifiers it
references (defined / called / imported names plus route-path string literals)
and matches those against the names/ids of nodes already in ``graph.json``.  The
matched node ids become the seeds fed to :mod:`cpg.retrieve`.

A deterministic ``ast`` pass is the primary path (free, offline, and consistent
with the repo's no-LLM static-analysis ethos).  An optional LLM fallback
(:func:`cpg.review_llm.llm_pick_nodes`) handles snippets that will not parse or
that name nothing present in the graph.
"""
from __future__ import annotations

import ast
import os
import textwrap

from . import model as M


# ---------------------------------------------------------------------------
# Entity extraction from a snippet
# ---------------------------------------------------------------------------

def extract_entities(snippet: str) -> dict:
    """Pull candidate identifiers out of *snippet* using ``ast``.

    Returns a dict::

        {
          "parse_ok": bool,        # False when the snippet would not parse
          "defined":  set[str],    # functions/classes defined in the snippet
          "called":   set[str],    # bare call targets, e.g. create_record(...)
          "attrs":    set[str],    # attribute call tails, e.g. self.validate() -> validate
          "imported": set[str],    # imported symbol names
          "routes":   set[str],    # normalised route-path literals ("/x/{p}")
        }

    Never raises on a malformed snippet — ``parse_ok`` is False instead.
    """
    defined: set[str] = set()
    called: set[str] = set()
    attrs: set[str] = set()
    imported: set[str] = set()
    routes: set[str] = set()

    tree = _try_parse(snippet)
    if tree is None:
        return {"parse_ok": False, "defined": defined, "called": called,
                "attrs": attrs, "imported": imported, "routes": routes}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                attrs.add(func.attr)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add((alias.asname or alias.name).split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.asname or alias.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value.strip()
            if s.startswith("/") and len(s) > 1:
                routes.add(M.normalize_path(s))

    return {"parse_ok": True, "defined": defined, "called": called,
            "attrs": attrs, "imported": imported, "routes": routes}


def _try_parse(snippet: str):
    """Parse *snippet* as a module, tolerating over-indented fragments.

    A chunk copied from inside a class/function is often over-indented or is a
    bare suite of statements.  We try, in order: raw parse, dedented parse, and
    a function-body wrap.  Returns the AST or None.
    """
    try:
        return ast.parse(snippet)
    except SyntaxError:
        pass
    body = textwrap.dedent(snippet)
    try:
        return ast.parse(body)
    except SyntaxError:
        pass
    wrapped = "def __snippet__():\n" + textwrap.indent(body, "    ")
    try:
        return ast.parse(wrapped)
    except SyntaxError:
        return None


# ---------------------------------------------------------------------------
# Node name/path indexes
# ---------------------------------------------------------------------------

# Node kinds whose `name` is a stable, snippet-visible identifier.
_NAMED_KINDS = ("Function", "Class", "Component", "Table")

# Attribute-call tails that are overwhelmingly stdlib / ORM / builtin chaining
# rather than calls to indexed project functions.  Skipped when matching
# attribute calls so e.g. ``db.add(x)`` / ``payload.get("k")`` / ``query.all()``
# do not seed unrelated functions that happen to share the name.
_ATTR_STOPLIST = {
    # dict / list / str / set / file builtins
    "get", "keys", "values", "items", "append", "pop", "extend", "insert",
    "remove", "update", "setdefault", "format", "join", "split", "strip",
    "lstrip", "rstrip", "lower", "upper", "encode", "decode", "replace",
    "startswith", "endswith", "find", "index", "count", "sort", "sorted",
    "copy", "add", "discard", "read", "write", "close", "seek", "flush",
    # common ORM / DB session + query chaining
    "query", "filter", "filter_by", "all", "first", "one", "one_or_none",
    "scalar", "scalars", "commit", "rollback", "refresh", "merge", "add_all",
    "delete", "execute", "fetchone", "fetchall", "begin", "options",
    "order_by", "group_by", "limit", "offset", "outerjoin",
    # pydantic / serialisation
    "json", "dict", "model_dump", "model_validate", "model_dump_json",
}


def _build_indexes(nodes: list[dict]):
    """Return (by_name, method_tail, route_paths) lookup indexes over *nodes*.

    by_name:     identifier -> [node id, ...]   (exact name match)
    method_tail: last qualname segment -> [Function id, ...]
                 e.g. "RecordService.create" registers tail "create"
    route_paths: normalised path -> [Route id, ...]
    """
    by_name: dict[str, list[str]] = {}
    method_tail: dict[str, list[str]] = {}
    route_paths: dict[str, list[str]] = {}
    for n in nodes:
        kind = n.get("kind", "")
        name = n.get("name", "")
        nid = n["id"]
        if kind in _NAMED_KINDS and name:
            by_name.setdefault(name, []).append(nid)
        if kind == "Function" and name:
            tail = name.rsplit(".", 1)[1] if "." in name else name
            method_tail.setdefault(tail, []).append(nid)
        if kind == "Route":
            path = n.get("path", "")
            if path:
                route_paths.setdefault(path, []).append(nid)
    return by_name, method_tail, route_paths


# ---------------------------------------------------------------------------
# Snippet -> seed node ids
# ---------------------------------------------------------------------------

def seeds_from_snippet(
    snippet: str,
    nodes: list[dict],
    *,
    use_llm: bool = False,
    catalog_limit: int = 1500,
) -> list[str]:
    """Return graph seed node ids referenced by *snippet*.

    Deterministic name-matching is tried first; entities the snippet *defines*
    anchor strongest, followed by called/imported symbols and route literals.
    When that finds nothing and *use_llm* is set, fall back to LLM extraction
    over a node catalog.
    """
    ent = extract_entities(snippet)
    by_name, method_tail, route_paths = _build_indexes(nodes)

    seeds: list[str] = []
    seen: set[str] = set()

    def add(ids: list[str]) -> None:
        for i in ids:
            if i not in seen:
                seen.add(i)
                seeds.append(i)

    # 1. entities the snippet defines (strongest anchor: the snippet IS this code)
    for name in ent["defined"]:
        add(by_name.get(name, []))
        add(method_tail.get(name, []))
    # 2. called / attribute-call targets
    for name in ent["called"]:
        add(by_name.get(name, []))
        add(method_tail.get(name, []))
    for name in ent["attrs"]:
        if name in _ATTR_STOPLIST:
            continue
        add(method_tail.get(name, []))
    # 3. imported symbols
    for name in ent["imported"]:
        add(by_name.get(name, []))
        add(method_tail.get(name, []))
    # 4. route-path literals
    for path in ent["routes"]:
        add(route_paths.get(path, []))

    if seeds:
        return seeds

    # Fallback: let the LLM pick ids from a compact catalog.
    if use_llm:
        from .review_llm import llm_pick_nodes
        catalog = [
            {"id": n["id"], "kind": n.get("kind", ""),
             "name": n.get("name", ""), "file": n.get("file", "")}
            for n in nodes
            if n.get("kind") in ("Function", "Class", "Route", "Component", "Table")
        ][:catalog_limit]
        valid = {c["id"] for c in catalog}
        return [i for i in llm_pick_nodes(snippet, catalog) if i in valid]

    return seeds


def seeds_from_changes(
    file: str,
    line_ranges: list[tuple[int, int]],
    nodes: list[dict],
) -> list[str]:
    """Return ids of nodes in *file* whose line span overlaps any changed range.

    Forward-looking locator for the eventual diff path: given a changed file and
    the ``(start, end)`` spans that changed, find the nodes covering those lines
    by ``[line_start, line_end]`` overlap.
    """
    rel = file.replace("\\", "/")
    # Normalise ranges: drop malformed entries, order each (start <= end).
    norm_ranges: list[tuple[int, int]] = []
    for r in line_ranges or []:
        try:
            a, b = int(r[0]), int(r[1])
        except (TypeError, ValueError, IndexError):
            continue
        norm_ranges.append((a, b) if a <= b else (b, a))
    if not norm_ranges:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for n in nodes:
        if n.get("file", "").replace("\\", "/") != rel:
            continue
        ns = n.get("line_start", n.get("line", 0)) or 0
        ne = n.get("line_end", ns) or ns
        if ne < ns:
            ns, ne = ne, ns
        for cs, ce in norm_ranges:
            if ns <= ce and cs <= ne:   # ranges overlap
                if n["id"] not in seen:
                    seen.add(n["id"])
                    out.append(n["id"])
                break
    return out


# ---------------------------------------------------------------------------
# Source text resolution (text only; slicing still keys off line_start/line_end)
# ---------------------------------------------------------------------------

def build_source_map(
    relpaths,
    repo_root: str,
    embedded: dict | None = None,
) -> dict[str, str]:
    """Build relpath -> source text, preferring *embedded* then the repo.

    *embedded* is the graph's own ``source_map`` (present only when built with
    ``--source-map``).  Anything missing is read from ``{repo_root}/{relpath}``.
    The returned map is consumed by ``cpg.retrieve.slice_node``, which looks up
    ``node["file"]`` verbatim — so keys are kept exactly as they appear on the
    nodes (Windows-built graphs use backslash paths); only the filesystem read
    is separator-normalised.
    """
    source_map: dict[str, str] = {}
    embedded = embedded or {}
    for rel in relpaths:
        if not rel or rel in source_map:
            continue
        norm = rel.replace("\\", "/")
        # Embedded maps (build.py --source-map) are keyed forward-slash; node
        # file fields may be backslash. Accept either, store under the node key.
        if rel in embedded:
            source_map[rel] = embedded[rel]
        elif norm in embedded:
            source_map[rel] = embedded[norm]
        elif repo_root:
            path = os.path.join(repo_root, *norm.split("/"))
            try:
                with open(path, encoding="utf-8") as f:
                    source_map[rel] = f.read()
            except (OSError, UnicodeDecodeError):
                continue
    return source_map
