"""Build additive per-file change + impact outputs for UI/downloads.

This module is intentionally side-effect free so Streamlit can call it to render
an extra output section without changing any existing report flow.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set


def _safe_sort(values: Set[str]) -> List[str]:
    return sorted(v for v in values if v)


def _line_count(diff_lines: List[str], prefix: str, header_prefix: str) -> int:
    return sum(1 for ln in diff_lines if ln.startswith(prefix) and not ln.startswith(header_prefix))


def _cluster_risk(cluster) -> str:
    # Local import keeps this module lightweight and avoids hard dependency loops.
    from .impact import cluster_risk

    label, _emoji = cluster_risk(cluster)
    return label


def build_per_file_change_impact(
    *,
    diff_files: List[Dict[str, Any]],
    code_graph,
    impact_reviews,
    per_file_findings: Dict[str, List[Any]],
    changed_nodes,
) -> List[Dict[str, Any]]:
    """Return one additive summary object per changed file.

    The shape is JSON-friendly and aimed at both UI display and file downloads.
    """
    by_file: Dict[str, Dict[str, Any]] = {}

    for f in diff_files or []:
        path = f.get("path", "")
        if not path:
            continue
        by_file[path] = {
            "file": path,
            "added_lines": _line_count(f.get("lines", []), "+", "+++"),
            "removed_lines": _line_count(f.get("lines", []), "-", "---"),
            "changed_nodes": [],
            "local_findings": len(per_file_findings.get(path, [])),
            "per_file_review_findings": [
                {
                    "severity": x.severity,
                    "category": x.category,
                    "title": x.title,
                    "file": x.file,
                    "line": x.line,
                }
                for x in (per_file_findings.get(path, []) or [])
            ],
            "impact": {
                "cluster_count": 0,
                "changed_symbols": [],
                "downstream_files": [],
                "downstream_consumers": [],
                "impact_findings": [],
                "highest_cluster_risk": "low",
            },
        }

    # Changed nodes mapped from diff->graph.
    for cn in changed_nodes or []:
        if not code_graph.has(cn.node_id):
            continue
        d = code_graph.node(cn.node_id)
        path = d.get("path", "")
        if path not in by_file:
            continue
        name = d.get("qualname") or d.get("name") or cn.node_id
        by_file[path]["changed_nodes"].append({
            "name": name,
            "kind": d.get("kind", "?"),
            "change_type": cn.change_type,
            "start_line": d.get("start_line", 0),
            "end_line": d.get("end_line", 0),
        })

    risk_rank = {"low": 0, "medium": 1, "high": 2}

    for rev in impact_reviews or []:
        cluster = rev.cluster
        risk = _cluster_risk(cluster)

        member_file_to_symbols: Dict[str, Set[str]] = {}
        for mid in cluster.members:
            if not code_graph.has(mid):
                continue
            md = code_graph.node(mid)
            mpath = md.get("path", "")
            if mpath not in by_file:
                continue
            member_file_to_symbols.setdefault(mpath, set()).add(
                md.get("qualname") or md.get("name") or mid
            )

        if not member_file_to_symbols:
            continue

        downstream_files: Set[str] = set()
        downstream_consumers: Set[str] = set()
        for ch in cluster.chains:
            c = ch.consumer
            if c.path:
                downstream_files.add(c.path)
            downstream_consumers.add(f"{c.qualname} ({c.path}:{c.start_line})")

        impact_findings_payload = [
            {
                "severity": f.severity,
                "category": f.category,
                "title": f.title,
                "file": f.file,
                "line": f.line,
            }
            for f in (rev.findings or [])
        ]

        for mpath, symbols in member_file_to_symbols.items():
            rec = by_file[mpath]["impact"]
            rec["cluster_count"] += 1
            rec["changed_symbols"].extend(_safe_sort(symbols))
            rec["downstream_files"].extend(_safe_sort(downstream_files))
            rec["downstream_consumers"].extend(_safe_sort(downstream_consumers))
            rec["impact_findings"].extend(impact_findings_payload)

            if risk_rank.get(risk, 0) > risk_rank.get(rec["highest_cluster_risk"], 0):
                rec["highest_cluster_risk"] = risk

    # Deduplicate list fields and sort results by file path.
    out: List[Dict[str, Any]] = []
    for path in sorted(by_file):
        item = by_file[path]
        item["changed_nodes"] = sorted(
            item["changed_nodes"],
            key=lambda x: (x.get("start_line", 0), x.get("name", "")),
        )
        item["per_file_review_findings"] = sorted(
            item["per_file_review_findings"],
            key=lambda x: (x.get("line", 0), x.get("severity", ""), x.get("title", "")),
        )

        impact = item["impact"]
        impact["changed_symbols"] = sorted(set(impact["changed_symbols"]))
        impact["downstream_files"] = sorted(set(impact["downstream_files"]))
        impact["downstream_consumers"] = sorted(set(impact["downstream_consumers"]))
        seen = set()
        unique_findings = []
        for f in impact["impact_findings"]:
            key = (f["file"], f["line"], f["severity"], f["title"])
            if key in seen:
                continue
            seen.add(key)
            unique_findings.append(f)
        impact["impact_findings"] = unique_findings

        out.append(item)
    return out


def render_per_file_change_impact_markdown(items: List[Dict[str, Any]]) -> str:
    """Render the per-file change impact summary as markdown."""
    lines: List[str] = ["# Per-file change impact report", ""]
    if not items:
        lines.append("No changed files to report.")
        return "\n".join(lines)

    for it in items:
        impact = it["impact"]
        lines.append(f"## {it['file']}")
        lines.append(
            f"- Changes: +{it['added_lines']} / -{it['removed_lines']}"
        )
        lines.append(f"- Local findings: {it['local_findings']}")
        lines.append(
            "- Impact summary: "
            f"clusters={impact['cluster_count']}, "
            f"downstream_files={len(impact['downstream_files'])}, "
            f"downstream_consumers={len(impact['downstream_consumers'])}, "
            f"impact_findings={len(impact['impact_findings'])}, "
            f"highest_cluster_risk={impact['highest_cluster_risk']}"
        )

        if it["changed_nodes"]:
            lines.append("- Changed nodes:")
            for n in it["changed_nodes"]:
                lines.append(
                    f"  - {n['kind']} {n['name']} "
                    f"({n['start_line']}-{n['end_line']}, {n['change_type']})"
                )

        if it["per_file_review_findings"]:
            lines.append("- Per-file review findings (Track A):")
            for f in it["per_file_review_findings"]:
                lines.append(
                    f"  - [{f['severity'].upper()}] {f['title']} "
                    f"({f['file']}:{f['line']}, {f['category']})"
                )

        if impact["downstream_files"]:
            lines.append("- Downstream files:")
            for p in impact["downstream_files"]:
                lines.append(f"  - {p}")

        if impact["impact_findings"]:
            lines.append("- Impact findings:")
            for f in impact["impact_findings"]:
                lines.append(
                    f"  - [{f['severity'].upper()}] {f['title']} "
                    f"({f['file']}:{f['line']}, {f['category']})"
                )
        lines.append("")
    return "\n".join(lines)