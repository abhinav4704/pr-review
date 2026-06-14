"""Build a compact architecture digest from the Neo4j code graph.

The whole repo never fits in an LLM context, so we pull the repo's *structure*
out of the graph (module map, cross-module coupling, cycles, hotspots,
entrypoints, dead code) and render it as a short markdown digest that a single
LLM call can review.
"""

from __future__ import annotations

from typing import Dict, List

import networkx as nx


def detect_module_cycles(module_edges: List[Dict]) -> List[List[str]]:
    """Find dependency cycles between top-level modules.

    The module graph is tiny, so we build it in-memory and use simple_cycles.
    """
    g = nx.DiGraph()
    for e in module_edges:
        g.add_edge(e["from_module"], e["to_module"])
    cycles = [c for c in nx.simple_cycles(g) if len(c) > 1]
    # Stable, shortest-first ordering
    return sorted(cycles, key=lambda c: (len(c), c))


def build_digest(store, pr_ref: str) -> Dict:
    """Collect all structural facts for the repo into one dict."""
    module_edges = store.module_edges(pr_ref)
    return {
        "pr_ref": pr_ref,
        "overview": store.kind_counts(pr_ref),
        "modules": store.module_sizes(pr_ref),
        "module_edges": module_edges,
        "cycles": detect_module_cycles(module_edges),
        "hotspots": store.top_fan_in(pr_ref),
        "god_classes": store.god_classes(pr_ref),
        "subclassed": store.top_subclassed(pr_ref),
        "entrypoints": store.entrypoints(pr_ref),
        "orphans": store.orphans(pr_ref),
    }


def _name(row: Dict) -> str:
    return row.get("qualname") or row.get("name") or "?"


def digest_to_markdown(d: Dict) -> str:
    """Render the digest dict as compact markdown — the text sent to the LLM."""
    lines: List[str] = []
    lines.append(f"# Repository structure digest (`{d['pr_ref']}`)\n")

    # Overview
    lines.append("## Overview")
    if d["overview"]:
        lines.append(
            "  ".join(f"{k}: {v}" for k, v in d["overview"].items())
        )
    else:
        lines.append("_no nodes_")
    lines.append("")

    # Modules
    lines.append("## Modules (top-level directories, by node count)")
    if d["modules"]:
        for m in d["modules"]:
            lines.append(f"- `{m['module']}` — {m['nodes']} nodes")
    else:
        lines.append("_none_")
    lines.append("")

    # Cross-module coupling
    lines.append("## Cross-module dependencies (imports)")
    if d["module_edges"]:
        for e in d["module_edges"]:
            lines.append(
                f"- `{e['from_module']}` -> `{e['to_module']}` ({e['weight']} imports)"
            )
    else:
        lines.append("_no cross-module imports detected_")
    lines.append("")

    # Cycles
    lines.append("## Module dependency cycles")
    if d["cycles"]:
        for c in d["cycles"]:
            lines.append("- " + " -> ".join(f"`{m}`" for m in c) + f" -> `{c[0]}`")
    else:
        lines.append("_none detected_")
    lines.append("")

    # Hotspots
    lines.append("## Hotspots (most-called functions/methods)")
    if d["hotspots"]:
        for h in d["hotspots"]:
            lines.append(f"- `{_name(h)}` ({h['kind']}, {h['path']}) — {h['fan']} callers")
    else:
        lines.append("_none_")
    lines.append("")

    # God classes
    lines.append("## Largest classes (by method count)")
    if d["god_classes"]:
        for c in d["god_classes"]:
            lines.append(f"- `{_name(c)}` ({c['path']}) — {c['methods']} methods")
    else:
        lines.append("_none_")
    lines.append("")

    # Inheritance
    lines.append("## Most-subclassed classes")
    if d["subclassed"]:
        for c in d["subclassed"]:
            lines.append(f"- `{_name(c)}` ({c['path']}) — {c['subs']} subclasses")
    else:
        lines.append("_none_")
    lines.append("")

    # Entrypoints
    lines.append("## Entry points (routes / events)")
    if d["entrypoints"]:
        for e in d["entrypoints"]:
            lines.append(f"- [{e['kind']}] `{_name(e)}` ({e['path']})")
    else:
        lines.append("_none detected_")
    lines.append("")

    # Orphans
    lines.append("## Possible dead code (uncalled, non-test functions)")
    if d["orphans"]:
        for o in d["orphans"]:
            lines.append(f"- `{_name(o)}` ({o['kind']}, {o['path']})")
    else:
        lines.append("_none_")
    lines.append("")

    return "\n".join(lines)
