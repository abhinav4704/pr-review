#!/usr/bin/env python3
"""Snippet-driven PR review over the multiplex code graph.

Given a raw code snippet (a chunk of a PR), this:
  1. extracts the entities the snippet references (cpg.locate),
  2. matches them to graph nodes -> seed node ids (you never supply an id),
  3. BFS-expands to connected nodes and slices each node's source via its
     line_start/line_end (cpg.retrieve),
  4. optionally sends snippet + connected code to an LLM for review
     (cpg.review_llm).

Examples
--------
  # Print the connected-code bundle (offline, no API key):
  python review_pr.py --snippet-file changed.py --graph graph.json --repo .

  # Full LLM PR review (needs OPENAI_API_KEY):
  python review_pr.py --snippet-file changed.py --graph graph.json --repo . --llm

  # Inline snippet:
  python review_pr.py --snippet "def create_record(...): ..." --repo .
"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--snippet", help="Raw code snippet to review")
    src.add_argument("--snippet-file", help="Path to a file holding the snippet")
    ap.add_argument("--graph", default="graph.json", help="Path to graph.json")
    ap.add_argument("--repo", default=".",
                    help="Repo root for reading source when the graph has no source_map")
    ap.add_argument("--llm", action="store_true",
                    help="Run the LLM review (needs OPENAI_API_KEY)")
    ap.add_argument("--use-llm-entities", action="store_true",
                    help="Fall back to LLM entity extraction when name-matching finds nothing")
    ap.add_argument("--hops-depth", type=int, default=None,
                    help="Fixed BFS depth (overrides risk-adaptive default)")
    ap.add_argument("--token-limit", type=int, default=6000,
                    help="Token budget for the assembled context bundle")
    args = ap.parse_args()

    snippet = args.snippet
    if args.snippet_file:
        try:
            with open(args.snippet_file, encoding="utf-8", errors="replace") as f:
                snippet = f.read()
        except OSError as e:
            sys.exit(f"Cannot read snippet file {args.snippet_file}: {e}")
    if not snippet or not snippet.strip():
        ap.error("snippet is empty")

    try:
        with open(args.graph, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        sys.exit(f"Graph file not found: {args.graph}\n"
                 f"Run: python build.py ... --out {args.graph}")
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(f"Could not read graph {args.graph}: {e}")

    nodes: list[dict] = data.get("nodes", [])
    edges: list[dict] = data.get("edges", [])
    embedded: dict[str, str] = data.get("source_map", {})
    if not nodes:
        sys.exit(f"Graph {args.graph} has no nodes. Rebuild it with build.py.")

    from cpg import locate, retrieve

    try:
        seeds = locate.seeds_from_snippet(snippet, nodes, use_llm=args.use_llm_entities)
    except RuntimeError as e:
        # e.g. LLM fallback requested without OPENAI_API_KEY
        sys.exit(f"Entity extraction failed: {e}")
    if not seeds:
        hint = "" if args.use_llm_entities else " Try --use-llm-entities."
        sys.exit("No graph nodes matched the snippet. It may not define/call any "
                 f"indexed entity.{hint}")

    # Source text: prefer the graph's embedded source_map, else read repo files.
    # Slicing itself always uses each node's line_start/line_end.
    relpaths = {n.get("file", "") for n in nodes}
    source_map = locate.build_source_map(relpaths, args.repo, embedded=embedded)

    if args.hops_depth is not None:
        # Fixed-depth expansion: BFS per seed, then slice.
        seen: set[str] = set()
        bundle: list[dict] = []
        for sid in seeds:
            sg_nodes, _ = retrieve.subgraph([sid], nodes, edges, max_hops=args.hops_depth)
            for n in sg_nodes:
                if n["id"] in seen:
                    continue
                seen.add(n["id"])
                bundle.append({
                    "id": n["id"], "kind": n.get("kind", ""),
                    "name": n.get("name", ""), "file": n.get("file", ""),
                    "source": retrieve.slice_node(n, source_map),
                    "hops": args.hops_depth,
                })
    else:
        bundle = retrieve.context_for(seeds, nodes, edges, source_map,
                                      token_limit=args.token_limit)

    nonempty = sum(1 for x in bundle if x.get("source"))
    print(f"# seeds ({len(seeds)}): {seeds}", file=sys.stderr)
    print(f"# bundle nodes: {len(bundle)} ({nonempty} with source)", file=sys.stderr)

    if bundle and nonempty == 0:
        print("# WARNING: matched nodes but no source could be read. The graph was "
              "likely built over a different/absent checkout and has no embedded "
              "source_map. Point --repo at the indexed source, or rebuild with "
              "`python build.py <repo> --source-map`.", file=sys.stderr)

    if not args.llm:
        print(json.dumps(bundle, indent=2, default=str))
        return

    if nonempty == 0:
        sys.exit("Refusing to call the LLM with no source context (see warning above).")

    from cpg.review_llm import generate_review
    try:
        print(generate_review(snippet, bundle))
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
