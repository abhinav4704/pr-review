"""CLI: python -m graph_rag.cli index <path> [--repo NAME] [--no-wipe]"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .config import neo4j_config
from .embeddings import (
    DEFAULT_EMBED_PROVIDER,
    EMBED_PROVIDERS,
    Embedder,
    default_embed_model,
    embed_nodes,
)
from .llm import DEFAULT_PROVIDER, PROVIDERS, SemanticLLM, default_model
from .pipeline import index_repo
from .retrieval import ask as retrieval_ask
from .semantic import LEVELS, enrich_identities, generate_flows
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

    vr = res.validation or {}
    if vr:
        print("\n  validation:")
        print(f"    ok={vr.get('ok', False)}  errors={len(vr.get('errors', []))}  warnings={len(vr.get('warnings', []))}")
        for e in vr.get("errors", []):
            print(f"    error: {e}")
        for w in vr.get("warnings", []):
            print(f"    warn:  {w}")

    if args.validation_report:
        with open(args.validation_report, "w", encoding="utf-8") as f:
            json.dump(vr, f, indent=2, sort_keys=True)
        print(f"\n  wrote validation report: {args.validation_report}")

    if args.fail_on_validation_error and not vr.get("ok", False):
        print("\n  validation failed: exiting with non-zero status")
        return 2
    print()
    return 0


def _cmd_semantic(args) -> int:
    if not args.path:
        print("no repo path: pass it as an argument or set REPO_PATH in .env")
        return 2
    repo = args.repo or os.path.basename(os.path.abspath(args.path.rstrip("/")))
    levels = tuple(args.levels.split(",")) if args.levels else LEVELS
    bad = [lvl for lvl in levels if lvl not in LEVELS]
    if bad:
        print(f"unknown level(s): {bad}; valid: {list(LEVELS)}")
        return 2

    model = args.model or default_model(args.provider)
    store = GraphStore(neo4j_config())
    llm = SemanticLLM(provider=args.provider, model=model)
    phase = "2B implementation-flow" if args.flows else "2A identity"
    print(f"\n  repo:     {repo}")
    print(f"  provider: {args.provider}   model: {model}"
          f"{'   (dry-run, no API calls)' if args.dry_run else ''}")
    print(f"  phase:    {phase}")
    if not args.flows:
        print(f"  levels:   {','.join(levels)}")
    print()
    try:
        if args.flows:
            generate_flows(repo, args.path, store, llm,
                           limit=args.limit, refresh=args.refresh, dry_run=args.dry_run)
        else:
            enrich_identities(repo, args.path, store, llm, levels=levels, limit=args.limit,
                              refresh=args.refresh, source_mode=args.source, dry_run=args.dry_run)
    finally:
        store.close()
    print()
    return 0


def _cmd_embed(args) -> int:
    repo = args.repo
    if not repo:
        print("no repo: pass --repo or set REPO_NAME in .env")
        return 2
    model = args.model or default_embed_model(args.provider)
    store = GraphStore(neo4j_config())
    embedder = Embedder(provider=args.provider, model=model)
    print(f"\n  repo:     {repo}")
    print(f"  provider: {args.provider}   model: {model}")
    print()
    try:
        res = embed_nodes(repo, store, embedder, limit=args.limit, refresh=args.refresh)
    finally:
        store.close()
    print(f"\n  embedded {res.embedded}/{res.total} identities   dim={res.dim}")
    print()
    return 0


def _cmd_ask(args) -> int:
    repo = args.repo
    if not repo:
        print("no repo: pass --repo or set REPO_NAME in .env")
        return 2
    root = args.path or os.environ.get("REPO_PATH") or ""
    store = GraphStore(neo4j_config())
    embedder = Embedder(provider=args.embed_provider,
                        model=args.embed_model or default_embed_model(args.embed_provider))
    llm = None
    if not args.no_llm:
        llm = SemanticLLM(provider=args.provider,
                          model=args.model or default_model(args.provider),
                          max_tokens=args.max_tokens)
    print(f"\n  repo:  {repo}")
    print(f"  embed: {embedder.provider}/{embedder.model}")
    print(f"  llm:   {'(none — retrieval only, no prune/answer)' if llm is None else llm.provider + '/' + llm.model}")
    try:
        retrieval_ask(args.question, repo, root, store, embedder, llm,
                      top_k=args.top_k, expand_top=args.expand)
    finally:
        store.close()
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
    idx.add_argument("--validation-report", default=None, help="write validation JSON report to this file")
    idx.add_argument("--fail-on-validation-error", action="store_true", help="exit non-zero if validation has errors")
    idx.set_defaults(func=_cmd_index)

    sem = sub.add_parser("semantic", help="enrich an indexed repo with LLM semantics")
    sem.add_argument("path", nargs="?", default=os.environ.get("REPO_PATH"),
                     help="repo root (same path used at index time; default: REPO_PATH from .env)")
    sem.add_argument("--repo", default=os.environ.get("REPO_NAME"),
                     help="repo name (default: REPO_NAME from .env, else dir name)")
    sem.add_argument("--provider", default=DEFAULT_PROVIDER, choices=PROVIDERS,
                     help="LLM provider (default: GRAPH_RAG_LLM_PROVIDER or anthropic)")
    sem.add_argument("--model", default=None, help="model id (default: GRAPH_RAG_LLM_MODEL or provider default)")
    sem.add_argument("--levels", default=None,
                     help=f"comma-separated subset of {','.join(LEVELS)} (default: all, bottom-up)")
    sem.add_argument("--source", default=os.environ.get("GRAPH_RAG_SOURCE_MODE", "auto"),
                     choices=("auto", "never", "always"),
                     help="raw-source inclusion (default: GRAPH_RAG_SOURCE_MODE or auto)")
    sem.add_argument("--flows", action="store_true",
                     help="Phase 2B: generate implementation flows (lazy) instead of identities")
    sem.add_argument("--limit", type=int, default=None, help="cap generations (smoke test)")
    sem.add_argument("--refresh", action="store_true", help="regenerate even if body_hash is unchanged")
    sem.add_argument("--dry-run", action="store_true", help="assemble + print the first prompt; no API calls")
    sem.set_defaults(func=_cmd_semantic)

    emb = sub.add_parser("embed", help="embed identities into vectors (Phase 3 vector leg)")
    emb.add_argument("--repo", default=os.environ.get("REPO_NAME"),
                     help="repo name (default: REPO_NAME from .env)")
    emb.add_argument("--provider", default=DEFAULT_EMBED_PROVIDER, choices=EMBED_PROVIDERS,
                     help="embed provider (default: GRAPH_RAG_EMBED_PROVIDER or local)")
    emb.add_argument("--model", default=None,
                     help="embed model id (default: GRAPH_RAG_EMBED_MODEL or provider default)")
    emb.add_argument("--limit", type=int, default=None, help="cap embeddings (smoke test)")
    emb.add_argument("--refresh", action="store_true", help="re-embed even if unchanged")
    emb.set_defaults(func=_cmd_embed)

    ask = sub.add_parser("ask", help="hybrid retrieval + LLM prune/answer loop over an enriched repo")
    ask.add_argument("question")
    ask.add_argument("--repo", default=os.environ.get("REPO_NAME"),
                     help="repo name (default: REPO_NAME from .env)")
    ask.add_argument("--path", default=os.environ.get("REPO_PATH"),
                     help="repo root for source in the context pack (default: REPO_PATH from .env)")
    ask.add_argument("--provider", default=DEFAULT_PROVIDER, choices=PROVIDERS,
                     help="LLM provider for prune/answer (default: GRAPH_RAG_LLM_PROVIDER or anthropic)")
    ask.add_argument("--model", default=None, help="LLM model id (default: provider default)")
    ask.add_argument("--max-tokens", type=int, default=2048, help="max tokens for the answer")
    ask.add_argument("--embed-provider", default=DEFAULT_EMBED_PROVIDER, choices=EMBED_PROVIDERS,
                     help="query-embedding provider (default: local)")
    ask.add_argument("--embed-model", default=None, help="query-embedding model (default: provider default)")
    ask.add_argument("--top-k", type=int, default=8, help="candidates per leg before fusion")
    ask.add_argument("--expand", type=int, default=5, help="top-N reranked nodes to expand neighbors from")
    ask.add_argument("--no-llm", action="store_true",
                     help="retrieval only: skip both prune passes and answer (no API/Bedrock creds needed)")
    ask.set_defaults(func=_cmd_ask)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
