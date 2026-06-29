"""Dependency hygiene scanner (declared vs imported + optional OSV enrichment)."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Dict, List, Set
from urllib.error import URLError
from urllib.request import Request, urlopen

from analyser.checks.common import should_exclude_path


def _normalize_pkg_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _declared_python_packages(repo_root: Path) -> Set[str]:
    declared: Set[str] = set()
    req = repo_root / "requirements.txt"
    if req.exists():
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pkg = line.split("==")[0].split(">=")[0].split("<=")[0].strip()
            if pkg:
                declared.add(_normalize_pkg_name(pkg))
    return declared


def _imported_python_modules(repo_root: Path) -> Set[str]:
    imported: Set[str] = set()
    for py_file in repo_root.rglob("*.py"):
        rel = py_file.relative_to(repo_root).as_posix()
        if should_exclude_path(rel):
            continue

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root:
                        imported.add(_normalize_pkg_name(root))
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root = node.module.split(".")[0]
                    if root:
                        imported.add(_normalize_pkg_name(root))
    return imported


def _query_osv(pkg: str, ecosystem: str = "PyPI") -> List[dict]:
    payload = json.dumps({"package": {"name": pkg, "ecosystem": ecosystem}}).encode("utf-8")
    req = Request(
        url="https://api.osv.dev/v1/query",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=6) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    vulns = data.get("vulns", []) if isinstance(data, dict) else []
    out = []
    for vuln in vulns[:5]:
        out.append(
            {
                "id": vuln.get("id", ""),
                "summary": vuln.get("summary", "") or vuln.get("details", "")[:160],
            }
        )
    return out


def scan_dependencies(repo_root: str, include_osv: bool = False) -> Dict[str, object]:
    """Scan declared/imported dependency mismatch and optionally query OSV."""
    root = Path(repo_root)

    declared = _declared_python_packages(root)
    imported = _imported_python_modules(root)

    # Local modules may look like dependencies; this keeps output useful and conservative.
    local_module_names = {
        p.stem.lower().replace("_", "-")
        for p in root.rglob("*.py")
        if p.is_file()
    }

    imported_external = {pkg for pkg in imported if pkg not in local_module_names}

    undeclared = sorted(imported_external - declared)
    unused_declared = sorted(declared - imported_external)

    vuln_findings: List[dict] = []
    osv_status = "disabled"
    if include_osv and declared:
        try:
            for pkg in sorted(declared):
                vulns = _query_osv(pkg)
                for vuln in vulns:
                    vuln_findings.append(
                        {
                            "severity": "high",
                            "package": pkg,
                            "id": vuln.get("id", ""),
                            "summary": vuln.get("summary", ""),
                        }
                    )
            osv_status = "ok"
        except (URLError, TimeoutError, OSError, ValueError):
            osv_status = "offline-or-failed"

    findings: List[dict] = []
    for pkg in undeclared:
        findings.append(
            {
                "severity": "medium",
                "type": "undeclared",
                "package": pkg,
                "message": "Imported in code but not declared in requirements.txt",
            }
        )
    for pkg in unused_declared:
        findings.append(
            {
                "severity": "low",
                "type": "unused",
                "package": pkg,
                "message": "Declared in requirements.txt but not imported in scanned source",
            }
        )

    findings.extend(vuln_findings)

    return {
        "findings": findings,
        "declared_count": len(declared),
        "imported_count": len(imported_external),
        "osv_status": osv_status,
    }
