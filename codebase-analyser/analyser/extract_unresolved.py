"""Missing-symbol extraction for unresolved in-repo imports."""

from __future__ import annotations

import ast
import builtins
from pathlib import Path
from typing import Dict, List, Set


BUILTIN_NAMES = set(dir(builtins))


def _module_index(repo_root: str) -> Dict[str, str]:
    root = Path(repo_root)
    index: Dict[str, str] = {}
    for py_file in root.rglob("*.py"):
        rel = py_file.relative_to(root).as_posix()
        module = rel[:-3].replace("/", ".")
        index[module] = rel
        if module.endswith(".__init__"):
            index[module[: -len(".__init__")]] = rel
    return index


def _resolve_module(current_module: str, module: str | None, level: int) -> str:
    pkg_parts = current_module.split(".")[:-1]

    if level > 0:
        # level=1 means current package, level=2 goes one level up, etc.
        up = level - 1
        if up > len(pkg_parts):
            return ""
        anchor = pkg_parts[: len(pkg_parts) - up]
    else:
        anchor = []

    suffix = (module or "").split(".") if module else []
    parts = [part for part in (anchor + suffix) if part]
    return ".".join(parts)


def _defs_by_file(code_graph) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    for node_id, node_data in code_graph.g.nodes(data=True):
        if node_data.get("kind") == "file":
            continue
        path = node_data.get("path", "")
        name = node_data.get("name", "")
        if path and name:
            out.setdefault(path, set()).add(name)
    return out


def extract_missing_symbols(repo_root: str, code_graph) -> List[dict]:
    """Detect in-repo from-import symbols that are missing in target modules."""
    root = Path(repo_root)
    mod_to_file = _module_index(repo_root)
    defs_by_file = _defs_by_file(code_graph)

    findings: List[dict] = []
    seen: Set[tuple] = set()

    for py_file in root.rglob("*.py"):
        rel = py_file.relative_to(root).as_posix()
        current_module = rel[:-3].replace("/", ".")

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if not node.names:
                continue

            target_module = _resolve_module(current_module, node.module, int(node.level or 0))
            if not target_module:
                continue

            target_file = mod_to_file.get(target_module)
            if not target_file:
                continue

            target_defs = defs_by_file.get(target_file, set())
            for alias in node.names:
                symbol = alias.name
                if symbol == "*":
                    continue
                if symbol in BUILTIN_NAMES:
                    continue
                if symbol in target_defs:
                    continue

                key = (rel, int(getattr(node, "lineno", 0) or 0), target_module, symbol)
                if key in seen:
                    continue
                seen.add(key)

                findings.append(
                    {
                        "severity": "high",
                        "path": rel,
                        "line": int(getattr(node, "lineno", 0) or 0),
                        "module": target_module,
                        "symbol": symbol,
                        "target_file": target_file,
                        "message": (
                            f"Symbol '{symbol}' is imported from '{target_module}' "
                            "but no matching definition was found in the target module."
                        ),
                    }
                )

    findings.sort(key=lambda item: (item["path"], item["line"], item["symbol"]))
    return findings
