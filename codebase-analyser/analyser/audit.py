"""Audit orchestrator for manual-first codebase analysis."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List

from .checks.architecture import analyze_architecture
from .checks.breakage import analyze_breakage
from .checks.deadcode import analyze_deadcode
from .extract_unresolved import extract_missing_symbols
from .graph_adapter import build_analyzer_graph, count_files_and_definitions
from .scanners.dependencies import scan_dependencies
from .scanners.secrets import scan_secrets


@dataclass
class AuditSummary:
    repo_root: str
    generated_at: str
    files: int
    definitions: int
    findings_total: int
    health_score: int


@dataclass
class AuditResult:
    summary: AuditSummary
    breakage: Dict[str, object] = field(default_factory=dict)
    deadcode: Dict[str, object] = field(default_factory=dict)
    architecture: Dict[str, object] = field(default_factory=dict)
    security: Dict[str, object] = field(default_factory=dict)
    dependencies: Dict[str, object] = field(default_factory=dict)


def _compute_health_score(
    breakage_count: int,
    missing_symbol_count: int,
    deadcode_count: int,
    cycle_count: int,
    secrets_count: int,
    dependency_count: int,
) -> int:
    score = 100
    score -= min(40, breakage_count * 2)
    score -= min(20, missing_symbol_count * 2)
    score -= min(30, deadcode_count // 3)
    score -= min(20, cycle_count * 3)
    score -= min(20, secrets_count * 2)
    score -= min(10, dependency_count)
    return max(0, score)


def run_audit(
    repo_root: str,
    depth: int = 2,
    top_n: int = 20,
    include_osv: bool = False,
) -> AuditResult:
    """Run deterministic whole-codebase analysis in-memory."""
    abs_root = os.path.abspath(repo_root)
    code_graph = build_analyzer_graph(abs_root, backend="primitive")

    breakage = analyze_breakage(code_graph=code_graph, depth=depth, top_n=top_n)
    missing_symbols = extract_missing_symbols(repo_root=abs_root, code_graph=code_graph)
    breakage["missing_symbols"] = missing_symbols
    deadcode = analyze_deadcode(code_graph=code_graph)
    architecture = analyze_architecture(code_graph=code_graph)
    security = scan_secrets(abs_root)
    dependencies = scan_dependencies(abs_root, include_osv=include_osv)

    breakage_count = len(breakage.get("blast_radius", []))
    missing_symbol_count = len(missing_symbols)
    deadcode_count = len(deadcode.get("orphans", []))
    cycle_count = len(architecture.get("import_cycles", []))
    secrets_count = len(security.get("findings", []))
    dependency_count = len(dependencies.get("findings", []))
    file_count, definition_count = count_files_and_definitions(code_graph)

    summary = AuditSummary(
        repo_root=abs_root,
        generated_at=datetime.now(timezone.utc).isoformat(),
        files=file_count,
        definitions=definition_count,
        findings_total=(
            breakage_count
            + missing_symbol_count
            + deadcode_count
            + cycle_count
            + secrets_count
            + dependency_count
        ),
        health_score=_compute_health_score(
            breakage_count=breakage_count,
            missing_symbol_count=missing_symbol_count,
            deadcode_count=deadcode_count,
            cycle_count=cycle_count,
            secrets_count=secrets_count,
            dependency_count=dependency_count,
        ),
    )

    return AuditResult(
        summary=summary,
        breakage=breakage,
        deadcode=deadcode,
        architecture=architecture,
        security=security,
        dependencies=dependencies,
    )


def format_audit_report(result: AuditResult) -> str:
    """Render a markdown report from an AuditResult."""
    lines: List[str] = []
    lines.append("# Codebase Audit Report")
    lines.append("")
    lines.append(f"- Repo: `{result.summary.repo_root}`")
    lines.append(f"- Generated (UTC): `{result.summary.generated_at}`")
    lines.append(f"- Files: `{result.summary.files}`")
    lines.append(f"- Definitions: `{result.summary.definitions}`")
    lines.append(f"- Findings total: `{result.summary.findings_total}`")
    lines.append(f"- Health score: `{result.summary.health_score}/100`")
    lines.append("")

    lines.append("## Breakage")
    blast = result.breakage.get("blast_radius", [])
    if not blast:
        lines.append("No blast-radius risks identified.")
    else:
        for item in blast[:20]:
            lines.append(
                "- "
                f"{item['severity'].upper()} {item['source_name']} "
                f"(`{item['source_path']}`): impacts {item['impacted_count']} dependents"
            )

    missing = result.breakage.get("missing_symbols", [])
    lines.append("")
    lines.append("### Missing Symbols")
    if not missing:
        lines.append("No unresolved in-repo symbol imports detected.")
    else:
        for item in missing[:30]:
            lines.append(
                f"- {item['path']}:{item['line']} imports missing `{item['symbol']}` "
                f"from `{item['module']}`"
            )
    lines.append("")

    lines.append("## Dead Code")
    orphans = result.deadcode.get("orphans", [])
    if not orphans:
        lines.append("No orphan function/method candidates found.")
    else:
        for orphan in orphans[:30]:
            lines.append(
                f"- {orphan['name']} (`{orphan['path']}:{orphan['start_line']}`)"
            )
    lines.append("")

    lines.append("## Architecture")
    cycles = result.architecture.get("import_cycles", [])
    lines.append(f"- Import cycles: `{len(cycles)}`")
    hotspots = result.architecture.get("hotspots", [])
    lines.append(f"- Hotspots tracked: `{len(hotspots)}`")
    entrypoints = result.architecture.get("entrypoints", [])
    lines.append(f"- Entrypoints: `{len(entrypoints)}`")
    lines.append("")

    if cycles:
        lines.append("### Import Cycles")
        for cycle in cycles[:10]:
            lines.append(f"- {' -> '.join(cycle)}")
        lines.append("")

    lines.append("## Security")
    secrets = result.security.get("findings", [])
    if not secrets:
        lines.append("No potential secrets detected.")
    else:
        for item in secrets[:30]:
            lines.append(
                f"- {item['severity'].upper()} {item['path']}:{item['line']} ({item['type']})"
            )
    lines.append("")

    lines.append("## Dependencies")
    dep_findings = result.dependencies.get("findings", [])
    lines.append(f"- OSV status: `{result.dependencies.get('osv_status', 'disabled')}`")
    if not dep_findings:
        lines.append("No dependency hygiene findings.")
    else:
        for item in dep_findings[:40]:
            category = item.get("type", "vulnerability")
            pkg = item.get("package", "")
            lines.append(
                f"- {item['severity'].upper()} [{category}] {pkg}: {item.get('message', item.get('summary', ''))}"
            )
    lines.append("")

    return "\n".join(lines)
