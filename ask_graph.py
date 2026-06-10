#!/usr/bin/env python3
"""CLI for querying the multiplex code graph.

Two modes
---------
1. LLM Cypher (default)
   Requires OPENAI_API_KEY and a running Neo4j.
   Translates a natural-language question into read-only Cypher and executes it.

2. Hop traversal  --hops / --seed
   Requires only graph.json (offline, no LLM, no Neo4j).
   Performs BFS hop traversal from one or more seed node IDs and prints matching
   nodes with their source slices.  Risk-adaptive depth is used by default:
   nodes touching high-risk edges (GUARDED_BY, WRITES_TABLE, DEPENDS_ON,
   CALLS_ENDPOINT) get depth=3; all others get depth=1.

Examples
--------
  # LLM Cypher mode (needs OPENAI_API_KEY):
  python ask_graph.py "Which routes are guarded by auth?" --neo4j bolt://localhost:7687 --user neo4j --password password

  # Hop traversal from a known node id (offline, graph.json only):
  python ask_graph.py --hops --seed "func::app/routes.py::chat_completions" --graph graph.json

  # Search by name fragment, then hop:
  python ask_graph.py --hops --name "login" --graph graph.json

  # Fixed hop depth instead of risk-adaptive:
  python ask_graph.py --hops --name "submit_review" --hops-depth 2 --graph graph.json
"""
from __future__ import annotations

import argparse
import json
import sys


def _hop_mode(args) -> None:
    from cpg import retrieve

    graph_path = args.graph
    try:
        with open(graph_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        sys.exit(f"Graph file not found: {graph_path}\nRun: python build.py ... --out {graph_path}")

    nodes: list[dict] = data.get("nodes", [])
    edges: list[dict] = data.get("edges", [])
    source_map: dict[str, str] = data.get("source_map", {})

    # Collect seeds from --seed (exact ids) or --name (substring match on name/id/file).
    seeds: list[str] = list(args.seed or [])
    if args.name:
        q = args.name.lower()
        for n in nodes:
            if (q in (n.get("name") or "").lower()
                    or q in (n.get("id") or "").lower()
                    or q in (n.get("file") or "").lower()):
                seeds.append(n["id"])

    if not seeds:
        sys.exit(
            "No seed nodes found. Use --seed <id> or --name <substring>.\n"
            "Tip: run with --list-kinds to see available node kinds and counts."
        )

    # Optional kind filter on seeds (--kind Function etc.).
    if args.kind:
        allowed = {k.strip() for k in args.kind.split(",")}
        node_kinds = {n["id"]: n.get("kind", "") for n in nodes}
        seeds = [s for s in seeds if node_kinds.get(s, "") in allowed]
        if not seeds:
            sys.exit(f"No seed nodes match kind filter: {args.kind}")

    # Per-seed hop depth.
    if args.hops_depth is not None:
        hop_map = {s: args.hops_depth for s in seeds}
    else:
        hop_map = {s: retrieve.compute_hops(s, edges) for s in seeds}

    # BFS per seed, collect results.
    seen: set[str] = set()
    output: list[dict] = []
    for sid in seeds:
        depth = hop_map[sid]
        sg_nodes, sg_edges = retrieve.subgraph([sid], nodes, edges, max_hops=depth)
        for n in sg_nodes:
            if n["id"] in seen:
                continue
            seen.add(n["id"])
            sliced = retrieve.slice_node(n, source_map) if source_map else ""
            record = {
                "id":       n["id"],
                "kind":     n.get("kind", ""),
                "name":     n.get("name", ""),
                "file":     n.get("file", ""),
                "line":     n.get("line_start", n.get("line", 0)),
                "line_end": n.get("line_end", 0),
                "hops_from_seed": depth,
            }
            if sliced:
                record["source"] = sliced
            output.append(record)

        # Also report edges visible in this subgraph.
        for e in sg_edges:
            record = {
                "edge": e.get("type"),
                "src":  e.get("src"),
                "dst":  e.get("dst"),
            }

    # Optionally filter output by kind.
    if args.kind:
        allowed = {k.strip() for k in args.kind.split(",")}
        output = [r for r in output if r.get("kind", "") in allowed]

    print(json.dumps(output, indent=2, default=str))
    print(f"\n# nodes returned: {len(output)}", file=sys.stderr)


def _list_kinds(args) -> None:
    graph_path = args.graph
    try:
        with open(graph_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        sys.exit(f"Graph file not found: {graph_path}")
    counts: dict[str, int] = {}
    for n in data.get("nodes", []):
        k = n.get("kind", "?")
        counts[k] = counts.get(k, 0) + 1
    edge_counts: dict[str, int] = {}
    for e in data.get("edges", []):
        t = e.get("type", "?")
        edge_counts[t] = edge_counts.get(t, 0) + 1
    print("Node kinds:")
    for k, c in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k}: {c}")
    print("Edge types:")
    for t, c in sorted(edge_counts.items(), key=lambda x: -x[1]):
        print(f"  {t}: {c}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Query the multiplex graph (LLM Cypher or BFS hop traversal)"
    )
    ap.add_argument("question", nargs="?", default=None,
                    help="Natural-language question (LLM Cypher mode)")

    # Hop mode flags
    ap.add_argument("--hops", action="store_true",
                    help="Use BFS hop traversal (no LLM/Neo4j needed)")
    ap.add_argument("--seed", action="append", metavar="NODE_ID",
                    help="Seed node ID(s) for hop traversal (repeatable)")
    ap.add_argument("--name", metavar="SUBSTR",
                    help="Match seed nodes by name/id/file substring")
    ap.add_argument("--kind", metavar="KIND[,KIND...]",
                    help="Filter results to these node kinds (comma-separated)")
    ap.add_argument("--hops-depth", type=int, default=None,
                    help="Fixed BFS depth (overrides risk-adaptive default)")
    ap.add_argument("--graph", default="graph.json",
                    help="Path to graph.json (default: graph.json)")
    ap.add_argument("--list-kinds", action="store_true",
                    help="Print node kind and edge type counts then exit")

    # LLM Cypher mode flags
    ap.add_argument("--neo4j", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="")
    ap.add_argument("--database", default="neo4j")
    ap.add_argument("--max-rows", type=int, default=200)

    args = ap.parse_args()

    if args.list_kinds:
        _list_kinds(args)
        return

    if args.hops or args.seed or args.name:
        _hop_mode(args)
        return

    # LLM Cypher mode
    if not args.question:
        ap.error("question is required for LLM Cypher mode (or use --hops)")

    from cpg.cypher_qa import ask_graph
    result = ask_graph(
        question=args.question,
        uri=args.neo4j,
        user=args.user,
        password=args.password,
        database=args.database,
        max_rows=args.max_rows,
    )
    print("Generated Cypher:\n")
    print(result.cypher)
    print("\nRows:")
    print(json.dumps(result.rows, indent=2, default=str))
    print(f"\nrow_count={result.row_count}")


if __name__ == "__main__":
    main()
