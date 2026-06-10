#!/usr/bin/env python3
"""Build a deterministic multiplex code knowledge graph.

Usage::

  python build.py REPO_ROOT [--backend SUBDIR] [--frontend SUBDIR]
                  [--out graph.json]
                  [--neo4j bolt://localhost:7687 --user neo4j --password pw --wipe]

No LLM, no summaries: pure static analysis.

Source map
----------
During both the backend and frontend file walks a ``source_map`` dict is built::

    {relpath: source_text, ...}

This is passed to ``cpg.retrieve.context_for`` when building LLM context from
the graph.  The map is attached to the ``graph.json`` output under the
``"source_map"`` top-level key only when ``--source-map`` is passed (the dict
can be large for big repos).

PR-head documentation (architecture)
--------------------------------------
See ``architecture.md`` in this directory for the full ontology, Cypher
validation queries, known failure modes, and guardrails.
"""
from __future__ import annotations
import argparse
import os
import sys

from cpg import backend as python_backend, frontend, resolve, store

FE_EXT = frontend.FE_EXT
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
             "build", ".next", ".mypy_cache", ".pytest_cache"}


def walk(root, exts):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(exts):
                yield os.path.join(dirpath, fn)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--backend", default="")
    ap.add_argument("--frontend", default="")
    ap.add_argument("--out", default="graph.json")
    ap.add_argument("--neo4j", default="")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="")
    ap.add_argument("--database", default="neo4j")
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--source-map", action="store_true",
                    help="Embed raw source text in graph.json (makes file larger)")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    backend_root = args.backend  # relative, used to compute dotted module names
    be_dir = os.path.join(repo, args.backend) if args.backend else repo
    fe_dir = os.path.join(repo, args.frontend) if args.frontend else repo

    per_file = []
    source_map: dict[str, str] = {}
    for path in walk(be_dir, (".py",)):
        rel = os.path.relpath(path, repo)
        try:
            src = open(path, encoding="utf-8", errors="replace").read()
        except OSError as e:
            print(f"  skip (read) {rel}: {e}", file=sys.stderr)
            continue
        try:
            payload = python_backend.extract_python(rel, src, backend_root)
        except SyntaxError as e:
            print(f"  skip (syntax) {rel}: {e}", file=sys.stderr)
            continue
        except Exception as e:  # never let one bad file abort the whole build
            print(f"  skip (extract) {rel}: {e}", file=sys.stderr)
            continue
        source_map[rel.replace("\\", "/")] = src
        per_file.append(payload)

    fe_files = []
    if args.frontend or fe_dir == repo:
        scanned = set(pf["relpath"] for pf in per_file)
        for path in walk(fe_dir, FE_EXT):
            rel = os.path.relpath(path, repo)
            if rel in scanned:
                continue
            try:
                src = open(path, encoding="utf-8", errors="replace").read()
                fe = frontend.extract_frontend(rel, src, ts_cwd=fe_dir)
            except Exception as e:  # e.g. node/typescript missing — skip, don't abort
                print(f"  skip (frontend) {rel}: {e}", file=sys.stderr)
                continue
            source_map[rel.replace("\\", "/")] = src
            fe_files.append(fe)

    nodes, edges = resolve.build(per_file, fe_files)

    st = store.stats(nodes, edges)
    print("Graph built:")
    print(f"  nodes: {st['nodes_total']}")
    for kind, cnt in sorted(st["by_node"].items()):
        print(f"    {kind}: {cnt}")
    print(f"  edges: {st['edges_total']}")
    for etype, cnt in sorted(st["by_edge"].items()):
        print(f"    {etype}: {cnt}")

    store.to_json(nodes, edges, args.out,
                  source_map=source_map if args.source_map else None)
    print(f"  wrote {args.out}")

    if args.neo4j:
        store.to_neo4j(nodes, edges, args.neo4j, args.user, args.password,
                       args.database, wipe=args.wipe)
        print(f"  loaded into Neo4j at {args.neo4j}")


if __name__ == "__main__":
    main()