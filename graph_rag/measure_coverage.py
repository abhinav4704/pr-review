"""No-DB resolution-coverage probe.

Runs discover -> extract -> resolve in memory (no Neo4j) and prints per-edge-type
coverage plus a CALLS breakdown by receiver shape. Used to gauge Stage 2 quality.

    ./.venv/bin/python measure_coverage.py <path> [--repo NAME]
"""
from __future__ import annotations

import argparse
import os
from collections import Counter, defaultdict

from graph_rag import extractors
from graph_rag.discovery import discover
from graph_rag.resolver import resolve


def _baseline_calls(nodes, refs):
    """Reproduce the pre-scope resolver for CALLS: global name match, Functions
    only, unique->resolved / many->ambiguous / none->unresolved."""
    from graph_rag.resolver import Coverage
    by_name = defaultdict(list)
    for n in nodes:
        if n.label in ("Class", "Function"):
            by_name[n.name].append(n)
    cov = Coverage()
    for r in refs:
        if r.type != "CALLS":
            continue
        cov.total += 1
        cands = by_name.get(r.target_name, [])
        wanted = [c for c in cands if c.label == "Function"] or cands
        if len(wanted) == 1:
            cov.resolved += 1
        elif len(wanted) > 1:
            cov.ambiguous += 1
        elif r.target_name in by_name:
            cov.unresolved += 1
        else:
            cov.external += 1
    return cov


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--repo", default=None)
    args = ap.parse_args()
    repo = args.repo or os.path.basename(os.path.abspath(args.path.rstrip("/")))

    files = discover(args.path)
    nodes, edges, refs = [], [], []
    for f in files:
        n, e, r = extractors.extract(f, repo)
        nodes += n
        edges += e
        refs += r

    # CALLS receiver-shape census (where the win comes from)
    shapes = Counter()
    for r in refs:
        if r.type == "CALLS":
            if r.recv in ("self", "cls"):
                shapes["self/cls"] += 1
            elif r.recv:
                shapes["recv.name()"] += 1
            else:
                shapes["bare()"] += 1

    # Baseline: old global bare-name behaviour on the SAME inputs (CALLS only).
    base = _baseline_calls(nodes, refs)

    _, out_edges, coverage = resolve(nodes, edges, refs, repo)

    print(f"\nrepo: {repo}   files: {len(files)}   nodes: {len(nodes)}   "
          f"resolved-edges: {len(out_edges)}")
    print("\nresolution coverage (% = resolved of in-repo targets, external excluded):")
    for rtype, cov in sorted(coverage.items()):
        print(f"  {rtype:<16} total={cov.total:<5} in-repo={cov.inrepo:<5} "
              f"resolved={cov.resolved:<5} ({cov.pct():5.1f}%)  "
              f"ambiguous={cov.ambiguous:<4} unresolved={cov.unresolved:<4} "
              f"external={cov.external}")

    new = coverage["CALLS"]
    print("\nCALLS — scope-aware lift vs old global bare-name match (in-repo targets only):")
    print(f"  old:  resolved={base.resolved:<5} ({base.pct():5.1f}%)  "
          f"ambiguous={base.ambiguous}")
    print(f"  new:  resolved={new.resolved:<5} ({new.pct():5.1f}%)  "
          f"ambiguous={new.ambiguous}")

    print("\nCALLS receiver shapes (call-site census):")
    for shape, c in shapes.most_common():
        print(f"  {shape:<14} {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
