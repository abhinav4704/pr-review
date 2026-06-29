"""CLI: python -m graph_rag.cli index <path> [--repo NAME] [--no-wipe]"""
from __future__ import annotations

import argparse
import os
import sys

from .config import neo4j_config
from .pipeline import index_repo
from .store import GraphStore


def _cmd_index(args) -> int:
    repo = args.repo or os.path.basename(os.path.abspath(args.path.rstrip("/")))
    store = GraphStore(neo4j_config())
    try:
        res = index_repo(args.path, repo, store, wipe=not args.no_wipe,
                         scip=not args.no_scip)
    finally:
        store.close()

    print(f"\n  repo:   {res.repo}")
    print(f"  files:  {res.files}")
    print(f"  nodes:  {res.nodes}   edges: {res.edges}")
    print(f"  time:   {res.seconds:.2f}s")
    print(f"  in db:  {res.db_counts}")
    sc = res.scip
    if sc.available:
        print(f"\n  SCIP ({sc.tool}): CALLS={sc.calls} OVERRIDES={sc.overrides} "
              f"EXTRACTED ({sc.mapped_defs}/{sc.inrepo_defs} symbols mapped)")
    print("\n  resolution coverage (heuristic name match; % = of in-repo targets):")
    for rtype, cov in sorted(res.coverage.items()):
        print(
            f"    {rtype:<16} total={cov.total:<6} "
            f"resolved={cov.resolved:<6} ({cov.pct():5.1f}%)  "
            f"ambiguous={cov.ambiguous:<5} unresolved={cov.unresolved:<5} "
            f"external={cov.external}"
        )
    print()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="graph_rag")
    sub = p.add_subparsers(dest="cmd", required=True)

    idx = sub.add_parser("index", help="index a repo into Neo4j")
    idx.add_argument("path")
    idx.add_argument("--repo", default=None, help="repo name (default: dir name)")
    idx.add_argument("--no-wipe", action="store_true", help="do not delete existing repo nodes first")
    idx.add_argument("--no-scip", action="store_true", help="skip SCIP; use only the heuristic resolver")
    idx.set_defaults(func=_cmd_index)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
