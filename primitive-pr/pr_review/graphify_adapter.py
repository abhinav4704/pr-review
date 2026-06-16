"""Optional Graphify-backed graph builder for primitive-pr.

This adapter lets primitive-pr consume Graphify's broader extractor/build stack
while keeping the local CodeGraph interface stable for review flows.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import networkx as nx

from .graph_contract import confidence_score, edge_relation, normalize_confidence


_SUPPORTED_EXTS = {
    ".py", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".go", ".rs", ".java",
    ".groovy", ".gradle", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
    ".rb", ".cs", ".kt", ".kts", ".scala", ".php", ".swift", ".lua",
    ".luau", ".toc", ".zig", ".ps1", ".psm1", ".ex", ".exs", ".m",
    ".mm", ".jl", ".f", ".f90", ".f95", ".f03", ".f08", ".vue",
    ".svelte", ".astro", ".dart", ".v", ".sv", ".svh", ".sql", ".md",
    ".mdx", ".qmd", ".pas", ".pp", ".dpr", ".dpk", ".lpr", ".inc",
    ".dfm", ".lfm", ".lpk", ".sh", ".bash", ".json", ".tf", ".tfvars",
    ".hcl", ".dm", ".dme", ".dmi", ".dmm", ".dmf", ".sln", ".slnx",
    ".csproj", ".fsproj", ".vbproj", ".razor", ".cshtml", ".cls", ".trigger",
}

_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".tox",
    "dist",
    "build",
    ".gradle",
    "target",
}

_LOC_RE = re.compile(r"(?:L)?(\d+)")
_LINE_TOKEN_RE = re.compile(r"L(\d+)")


def _is_test_path(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    return (
        os.path.basename(p).startswith("test_")
        or os.path.basename(p).endswith("_test.py")
        or "/tests/" in p or "/test/" in p or "/spec/" in p
    )


def _lang_from_path(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".java": "java",
        ".go": "go",
    }.get(ext, "unknown")


def _extract_line_range(source_location: object) -> Tuple[int, int]:
    if not source_location:
        return 0, 0
    s = str(source_location)
    line_tokens = [int(m.group(1)) for m in _LINE_TOKEN_RE.finditer(s)]
    if line_tokens:
        if len(line_tokens) == 1:
            return line_tokens[0], line_tokens[0]
        return line_tokens[0], line_tokens[1]

    nums = [int(m.group(1)) for m in _LOC_RE.finditer(s)]
    if not nums:
        return 0, 0
    if len(nums) == 1:
        return nums[0], nums[0]
    return nums[0], nums[1]


def _node_kind(file_type: str, label: str, source_file: str = "", node_id: str = "") -> str:
    low = label.lower()
    stem = Path(source_file).stem.lower() if source_file else ""
    if source_file and node_id and stem and str(node_id).lower() == stem:
        return "file"
    if source_file and low in {
        "py", "js", "jsx", "mjs", "ts", "tsx", "java", "go", "rs",
        "c", "cpp", "cs", "php", "rb", "swift", "kt", "scala", "sh",
        "sql", "md", "json", "yaml", "yml", "xml", "html", "css",
    }:
        return "file"
    if source_file and any(low.endswith(ext) for ext in (
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs",
        ".php", ".rb", ".cs", ".kt", ".scala", ".sh", ".sql", ".md"
    )):
        return "file"
    if file_type == "code" and label.endswith("()"):
        return "function"
    if any(x in low for x in ("route", "endpoint", "handler")):
        return "route"
    if any(x in low for x in ("event", "listener", "subscriber")):
        return "event"
    if file_type == "code":
        return "class" if "." in label else "function"
    return file_type or "concept"


def _list_supported_files(root: str, max_files: int) -> List[Path]:
    out: List[Path] = []
    root_path = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            suffix = p.suffix.lower()
            if suffix not in _SUPPORTED_EXTS and not p.name.endswith(".blade.php"):
                continue
            try:
                rel = p.relative_to(root_path)
            except ValueError:
                continue
            out.append(Path(rel.as_posix()))
            if len(out) >= max_files:
                return out
    return out


def _sync_edge_contract(g: nx.DiGraph) -> None:
    for _u, _v, data in g.edges(data=True):
        relation = edge_relation(data)
        if relation:
            data.setdefault("relation", relation)
            data.setdefault("type", relation)
        raw_conf = data.get("confidence", "INFERRED")
        data.setdefault("confidence_kind", normalize_confidence(raw_conf))
        data.setdefault("confidence_score", confidence_score(raw_conf))


def try_build_with_graphify(root: str, max_files: int = 5000) -> Optional[Tuple[nx.DiGraph, Dict[str, int], Dict[str, List[str]]]]:
    """Build a graph via Graphify extract/build and adapt it to primitive fields.

    Returns (graph, lang_counts, by_simple) or None when Graphify is unavailable.
    """
    try:
        from .vendor_graph.build import build_from_json
        from .vendor_graph.extract import extract as graphify_extract
    except Exception:
        return None

    root_real = os.path.realpath(root)

    paths = _list_supported_files(root, max_files)
    if not paths:
        return nx.DiGraph(), {}, {}

    abs_paths = [Path(root) / p for p in paths]
    extraction = graphify_extract(abs_paths, cache_root=Path(root), parallel=False)
    built = build_from_json(extraction, directed=True, root=Path(root))

    out = nx.DiGraph()
    lang_counts: Dict[str, int] = {}
    by_simple: Dict[str, List[str]] = {}

    for node_id, data in built.nodes(data=True):
        label = str(data.get("label") or node_id)
        source_file = str(data.get("source_file") or "")
        if source_file:
            sf_real = os.path.realpath(source_file)
            if os.path.isabs(sf_real):
                try:
                    source_file = os.path.relpath(sf_real, root_real).replace("\\", "/")
                except ValueError:
                    source_file = sf_real.replace("\\", "/")
        start, end = _extract_line_range(data.get("source_location"))
        lang = _lang_from_path(source_file)
        if source_file:
            lang_counts[lang] = lang_counts.get(lang, 0) + 1

        kind = _node_kind(
            str(data.get("file_type", "")),
            label,
            source_file,
            str(node_id),
        )
        name = label[:-2] if label.endswith("()") else label.split(".")[-1]
        qualname = label[:-2] if label.endswith("()") else label

        attrs = {
            "kind": kind,
            "path": source_file,
            "name": name,
            "qualname": qualname,
            "start_line": start,
            "end_line": end,
            "lang": lang,
            "is_test": _is_test_path(source_file),
            "label": label,
            "file_type": data.get("file_type"),
            "source_file": source_file,
            "source_location": data.get("source_location"),
        }
        out.add_node(str(node_id), **attrs)
        if name:
            by_simple.setdefault(name, []).append(str(node_id))

    for u, v, edata in built.edges(data=True):
        payload = dict(edata)
        relation = edge_relation(payload)
        if relation:
            payload.setdefault("relation", relation)
            payload.setdefault("type", relation)
        out.add_edge(str(u), str(v), **payload)

    _sync_edge_contract(out)
    return out, lang_counts, by_simple
