"""CLI: python -m graph_rag.cli index <path> [--repo NAME] [--no-wipe]

Package layout (post-split):
  graph_core/  shared structural pipeline (extraction, resolution, storage) —
               used by both the analyzer and the RAG retrieval tool.
  analyzer/    the 2-agent code-analysis tool (Agent A = correctness+impact,
               Agent B = taint composition + architecture/layering). No
               identity/flow generation here — reads source + graph directly.
  rag/         the separate retrieval-augmented-generation tool (identities,
               implementation flows, embeddings, hybrid ask loop).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .graph_core.config import neo4j_config
from .graph_core.pipeline import index_repo
from .graph_core.store import GraphStore
from .graph_core.findings import Finding, dedupe, collapse_cross_category_duplicates
from .graph_core.llm import DEFAULT_PROVIDER, PROVIDERS, SemanticLLM, default_model

from .analyzer import agents as agents_mod
from .analyzer import taint as taint_mod
from .analyzer import scoring as scoring_mod

from .rag.embeddings import (
    DEFAULT_EMBED_PROVIDER,
    EMBED_PROVIDERS,
    Embedder,
    default_embed_model,
    embed_nodes,
)
from .rag.retrieval import ask as retrieval_ask
from .rag.semantic import LEVELS, enrich_identities, generate_flows


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
    scj = res.scip_java
    if scj.available:
        print(f"\n  SCIP-Java ({scj.tool}): CALLS={scj.calls} OVERRIDES={scj.overrides} "
              f"EXTRACTED ({scj.mapped_defs}/{scj.inrepo_defs} symbols mapped)")
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


def _cmd_analyze(args) -> int:
    """Agent A (correctness + impact) + Agent B (taint composition + qualify +
    architecture/layering), then blast-radius + severity scoring. Replaces the
    old separate analyze/taint/architecture CLI commands — one pass, two
    agents, reading source + graph directly (no identity/flow generation)."""
    if not args.path:
        print("no repo path: pass it as an argument or set REPO_PATH in .env")
        return 2
    repo = args.repo or os.path.basename(os.path.abspath(args.path.rstrip("/")))
    store = GraphStore(neo4j_config())
    llm = None
    if not args.no_llm:
        model = args.model or default_model(args.provider)
        llm = SemanticLLM(provider=args.provider, model=model)
    try:
        print(f"\n  repo: {repo}")

        print("  Agent B, pass 1: deterministic sink tagging + taint composition (no LLM)")
        tag_stats = taint_mod.run_taint_pass(args.path, repo, store, limit=args.limit)
        det_findings = taint_mod.find_taint_findings(store, repo, max_hops=args.max_hops)
        print(f"    functions_processed={tag_stats['functions_processed']}  sink_hits={tag_stats['sink_hits']}  "
              f"deterministic_findings={len(det_findings)}")
        stage1 = [
            Finding(category="security", subcategory=fnd["subcategory"], source="graph_proven",
                   owning_fqn=fnd["owning_fqn"], file=fnd["file"], line=fnd["line"],
                   message=fnd["message"], confidence=fnd.get("confidence", "HIGH"))
            for fnd in det_findings
        ]

        agent_findings: list[Finding] = []
        qualified: list[Finding] = []
        arch_findings: list[Finding] = []
        if llm is not None:
            print(f"  Agent A: correctness + impact, chunked over the repo "
                  f"({args.provider}/{args.model or default_model(args.provider)})")
            agent_findings = agents_mod.run_agent_a_scan(store, args.path, repo, llm, file_limit=args.limit)
            print(f"    findings={len(agent_findings)}")

            print("  Agent B, pass 2: taint qualify (reads raw chain source, not flow prose)")
            qualified = taint_mod.run_taint_qualify(store, repo, args.path, llm,
                                                    max_hops=args.max_hops, limit=args.qualify_limit)
            print(f"    confirmed true_positive={len(qualified)}")

            print("  Agent B, pass 3: architecture/layering (shape catalogue -> flag -> deep-dive raw code)")
            arch = taint_mod.run_architecture_pass(store, repo, args.path, llm, shape_limit=args.shape_limit)
            arch_findings = arch["findings"]
            print(f"    shapes_total={arch['shapes_total']}  shapes_flagged={arch['shapes_flagged']}  "
                  f"findings={len(arch_findings)}")
        else:
            print("  Agent A + Agent B qualify/architecture — skipped (--no-llm)")

        all_findings = collapse_cross_category_duplicates(dedupe(stage1 + agent_findings + arch_findings))
        print(f"  scoring: blast radius + qualify + severity "
              f"({'no LLM qualify — raw blast counts' if llm is None else 'LLM qualify'})")
        scored = scoring_mod.score_findings(store, repo, all_findings, llm=llm)

        print(f"\n  total scored findings: {len(scored)}\n")
        for f in scored:
            print(f"    {f.severity_label:8} {f.severity:5.2f}  [{f.category}/{f.subcategory}] "
                  f"{f.owning_fqn} ({f.file}:{f.line}) blast={f.blast_count}/{f.qualified_blast_count}")
            print(f"      {f.message}")

        if qualified:
            print(f"\n  Agent B qualify — LLM-confirmed true positives (separate list): {len(qualified)}\n")
            for f in qualified:
                print(f"    [{f.subcategory}] {f.owning_fqn} ({f.file}:{f.line}) — {f.message}")

        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump({
                    "scored": [f.to_dict() for f in scored],
                    "agent_b_qualified": [f.to_dict() for f in qualified],
                }, fh, indent=2, sort_keys=True)
            print(f"\n  wrote findings JSON: {args.json}")
    finally:
        store.close()
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
    print(f"  phase:    {phase} (RAG-only — not used by the analyzer)")
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

    idx = sub.add_parser("index", help="index a repo into Neo4j (shared graph, used by analyzer + rag)")
    idx.add_argument("path")
    idx.add_argument("--repo", default=None, help="repo name (default: dir name)")
    idx.add_argument("--no-wipe", action="store_true", help="do not delete existing repo nodes first")
    idx.add_argument("--no-scip", action="store_true", help="skip SCIP; use only the heuristic resolver")
    idx.add_argument("--validation-report", default=None, help="write validation JSON report to this file")
    idx.add_argument("--fail-on-validation-error", action="store_true", help="exit non-zero if validation has errors")
    idx.set_defaults(func=_cmd_index)

    ana = sub.add_parser("analyze", help="2-agent analysis: Agent A (correctness+impact) + Agent B (taint+architecture)")
    ana.add_argument("path", nargs="?", default=os.environ.get("REPO_PATH"),
                     help="repo root (same path used at index time; default: REPO_PATH from .env)")
    ana.add_argument("--repo", default=os.environ.get("REPO_NAME"),
                     help="repo name (default: REPO_NAME from .env, else dir name)")
    ana.add_argument("--provider", default=DEFAULT_PROVIDER, choices=PROVIDERS,
                     help="LLM provider for both agents (default: GRAPH_RAG_LLM_PROVIDER or anthropic)")
    ana.add_argument("--model", default=None, help="model id (default: GRAPH_RAG_LLM_MODEL or provider default)")
    ana.add_argument("--limit", type=int, default=None, help="cap Agent A files / sink-tagging functions (smoke test)")
    ana.add_argument("--max-hops", type=int, default=6, help="max taint-composition hops from source to sink")
    ana.add_argument("--qualify-limit", type=int, default=None, help="cap findings Agent B qualifies (cost control)")
    ana.add_argument("--shape-limit", type=int, default=None, help="cap distinct shapes sent to Agent B's architecture stage 1")
    ana.add_argument("--no-llm", action="store_true",
                     help="dry run: deterministic taint findings only, no Agent A, no qualify/architecture (no API/Bedrock creds needed)")
    ana.add_argument("--json", default=None, help="write scored + qualified findings JSON to this path")
    ana.set_defaults(func=_cmd_analyze)

    sem = sub.add_parser("semantic", help="[RAG] enrich an indexed repo with LLM identities/flows (not used by analyzer)")
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

    emb = sub.add_parser("embed", help="[RAG] embed identities into vectors")
    emb.add_argument("--repo", default=os.environ.get("REPO_NAME"),
                     help="repo name (default: REPO_NAME from .env)")
    emb.add_argument("--provider", default=DEFAULT_EMBED_PROVIDER, choices=EMBED_PROVIDERS,
                     help="embed provider (default: GRAPH_RAG_EMBED_PROVIDER or local)")
    emb.add_argument("--model", default=None,
                     help="embed model id (default: GRAPH_RAG_EMBED_MODEL or provider default)")
    emb.add_argument("--limit", type=int, default=None, help="cap embeddings (smoke test)")
    emb.add_argument("--refresh", action="store_true", help="re-embed even if unchanged")
    emb.set_defaults(func=_cmd_embed)

    ask = sub.add_parser("ask", help="[RAG] hybrid retrieval + LLM prune/answer loop over an enriched repo")
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
