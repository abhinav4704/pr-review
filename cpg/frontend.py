"""Frontend extraction using TypeScript AST (Node subprocess).

This module parses JS/TS/JSX/TSX source via TypeScript compiler API
(`typescript` package) through `frontend_ts_ast.js`, then maps parsed calls and
components into the graph model.

Edges emitted:
    CONTAINS       file -> fecall | file -> component
    RENDERS        component -> component  (placeholder; resolved in resolve.py)
    USES_COMPONENT component -> component  (placeholder)
    (CALLS_ENDPOINT fecall -> route is created later in resolve.py by matching)

Requirements:
    - `node` executable on PATH
    - `typescript` package resolvable by node (`require('typescript')`)
"""
from __future__ import annotations
import json
import os
import re
import subprocess
from cpg import model as M

FE_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs")

_TS_AST_SCRIPT = os.path.join(os.path.dirname(__file__), "frontend_ts_ast.js")

# Known HTTP-client receiver names used to filter _AXIOS matches.
# KNOWN_FAILURE: if your axios instance is named outside this set, calls through
# it will be silently skipped.  Add project-specific names here as needed.
_AXIOS_RECEIVERS = frozenset({
    "axios", "api", "http", "client", "request", "instance",
    "apiClient", "httpClient", "axiosInstance", "service",
})

def _normalize_fe_url(raw: str) -> tuple[str | None, bool]:
    """Return (normalized_path, resolved). Strips origin, collapses ${..} -> {p}."""
    url = raw.strip()
    # template expressions -> param placeholder
    had_expr = "${" in url
    url = re.sub(r"\$\{[^}]*\}", "{p}", url)
    # drop protocol+host if absolute
    m = re.match(r"^https?://[^/]+(/.*)$", url)
    if m:
        url = m.group(1)
    # a leading base-url variable (e.g. `${API}`) collapsed to {p}; strip it and
    # keep the real path that follows. ${API}/api/x -> /api/x
    if url.startswith("{p}"):
        rest = url[len("{p}"):]
        if rest.startswith("/"):
            url = rest
        else:
            return (url, False)
    if not url.startswith("/"):
        # could be a relative path or a bare variable; only keep if path-like
        if "/" not in url:
            return (None, False)
        url = "/" + url
    # strip query string
    url = url.split("?")[0]
    return (M.normalize_path(url), not had_expr)


def _extract_ts_ast(relpath: str, source: str, ts_cwd: str | None = None) -> dict:
    """Run Node+TypeScript AST extractor and return parsed JSON payload."""
    try:
        proc = subprocess.run(
            ["node", _TS_AST_SCRIPT, relpath],
            input=source,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            cwd=ts_cwd,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Node.js executable not found. Install Node.js to use TS AST frontend extraction."
        ) from exc

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            "TypeScript AST extraction failed. Ensure `typescript` is installed for node. "
            f"Details: {msg}"
        )

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON from TS AST extractor for {relpath}"
        ) from exc


def extract_frontend(relpath: str, source: str, ts_cwd: str | None = None) -> dict:
    nodes: list[dict] = []
    edges: list[dict] = []
    fnode = M.file_id(relpath)
    total_lines = source.count("\n") + 1
    nodes.append(M.node(fnode, "File", os.path.basename(relpath), relpath, 1,
                        line_end=total_lines, side="frontend"))
    seen_calls: set[str] = set()

    ast = _extract_ts_ast(relpath, source, ts_cwd=ts_cwd)

    for call in ast.get("calls", []):
        method = (call.get("method") or "GET").upper()
        raw = call.get("raw", "")
        receiver = call.get("receiver", "")
        if call.get("kind") == "axios" and receiver and receiver not in _AXIOS_RECEIVERS:
            continue

        path, resolved = _normalize_fe_url(raw)
        if not path:
            continue

        ln = int(call.get("line_start", 1) or 1)
        ln_end = int(call.get("line_end", ln) or ln)
        cid = M.fe_call_id(relpath, ln, method, path)
        if cid in seen_calls:
            continue
        seen_calls.add(cid)
        nodes.append(M.node(cid, "FrontendCall", f"{method} {path}", relpath, ln,
                            line_end=ln_end,
                            method=method, path=path, resolved=resolved, raw=raw))
        edges.append(M.edge(fnode, cid, "CONTAINS"))

    seen_comps: set[str] = set()
    for comp in ast.get("components", []):
        name = comp.get("name", "")
        if not name or name in seen_comps:
            continue
        seen_comps.add(name)
        ln = int(comp.get("line_start", 1) or 1)
        ln_end = int(comp.get("line_end", ln) or ln)
        cid = M.component_id(relpath, name)
        nodes.append(M.node(cid, "Component", name, relpath, ln, line_end=ln_end))
        edges.append(M.edge(fnode, cid, "CONTAINS"))

    for r in ast.get("renders", []):
        parent = r.get("parent", "")
        child = r.get("child", "")
        if not parent or not child:
            continue
        parent_id = M.component_id(relpath, parent)
        edges.append(M.edge(f"__comp__::{parent_id}",
                            f"__compref__::{child}",
                            "RENDERS",
                            _parent=parent_id, _child_name=child))

    return {"nodes": nodes, "edges": edges, "relpath": relpath}
