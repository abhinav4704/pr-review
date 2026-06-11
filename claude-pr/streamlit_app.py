"""Streamlit UI — full v2 with specialist agents, per-file findings, embedding index.

Run:
    pip install -r requirements.txt
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Ensure local package imports (pr_review.*) resolve even if launched from a
# different working directory.
APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

st.set_page_config(
    page_title="PR Review · Nova Pro",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded",
)

ss = st.session_state
ss.setdefault("gh", None)
ss.setdefault("user", None)
ss.setdefault("repos", [])
ss.setdefault("overall", None)       # OverallResult
ss.setdefault("report_md", "")
ss.setdefault("dossier", "")
ss.setdefault("graph_cache", {})     # sha -> (CodeGraph, EmbeddingIndex|None)

RISK_COLORS = {
    "low": "#1D9E75",
    "medium": "#BA7517",
    "high": "#D85A30",
    "critical": "#A32D2D",
}
SEV_BADGE = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    "info": "⚪",
}


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("🔗 GitHub connection")
    pat = st.text_input("Personal access token", type="password",
                        help="Needs `repo` scope for private repos.")
    if st.button("Connect", use_container_width=True):
        from pr_review.github_client import GitHubClient, GitHubError
        try:
            gh = GitHubClient(pat)
            ss.user = gh.whoami()
            ss.gh = gh
            with st.spinner("Loading repos..."):
                ss.repos = gh.list_repos()
            st.success(f"✓ {ss.user} · {len(ss.repos)} repos")
        except GitHubError as e:
            ss.gh = None
            st.error(str(e))

    st.divider()
    st.header("⚙️ Review options")

    dossier_only = st.toggle(
        "Dossier only (no LLM)",
        value=False,
        help="Build context without calling Nova — no AWS needed.",
    )

    with st.expander("Model settings"):
        model_id = st.text_input("Nova model id", value="us.amazon.nova-pro-v1:0")
        region = st.text_input("AWS region", value="us-east-1")
        token_budget = st.slider("Token budget per file", 2000, 30000, 8000, step=500)
        max_output = st.slider("Max output tokens", 1024, 5000, 4096, step=256)

    with st.expander("Agent selection"):
        st.caption("Deselect agents you don't need to save cost/time.")
        run_security = st.checkbox("Security", value=True)
        run_correctness = st.checkbox("Correctness / regression", value=True)
        run_performance = st.checkbox("Performance", value=True)
        run_api = st.checkbox("API & DB contracts", value=True)
        run_tests = st.checkbox("Test coverage", value=True)
        run_arch = st.checkbox("Architecture", value=True)

    with st.expander("Quality controls"):
        verify = st.toggle("Evidence verifier pass", value=True,
                           help="Second pass that drops unsupported findings.")
        use_embeddings = st.toggle(
            "Embedding index (semantic search)",
            value=True,
            help="Builds a local embedding index for semantic context retrieval. "
                 "Requires sentence-transformers.",
        )

    with st.expander("AWS credentials (optional)"):
        st.caption("Leave blank to use the default boto3 chain.")
        aws_key = st.text_input("Access key id", type="password")
        aws_secret = st.text_input("Secret access key", type="password")
        aws_token = st.text_input("Session token", type="password")

    with st.expander("Neo4j (optional)"):
        st.caption("Set to persist the graph to Neo4j for cross-PR queries.")
        neo4j_uri = st.text_input("Neo4j URI", value="", placeholder="bolt://localhost:7687")
        neo4j_user = st.text_input("User", value="neo4j")
        neo4j_pass = st.text_input("Password", type="password")


# ─────────────────────────────────────────────────────────────────────────────
# Main area
# ─────────────────────────────────────────────────────────────────────────────
st.title("Context-aware PR review")
st.caption(
    "Multi-language graph · adaptive blast radius · specialist agents · "
    "per-file findings · evidence verifier · Nova Pro"
)

if ss.gh is None:
    st.info("Enter a GitHub PAT in the sidebar and click **Connect**.")
    st.stop()

# repo + mode
repo = st.selectbox("Repository", ss.repos, index=0 if ss.repos else None)
mode = st.radio("Review target", ["Pull request", "Compare branches"], horizontal=True)

target_diff = None
head_repo = repo
head_ref = None
pr_label = ""

from pr_review.github_client import GitHubError

if mode == "Pull request":
    state_filter = st.radio("PR state", ["open", "closed", "all"], horizontal=True)
    try:
        pulls = ss.gh.list_pulls(repo, state=state_filter)
    except GitHubError as e:
        st.error(str(e))
        pulls = []
    if not pulls:
        st.warning("No pull requests found.")
        st.stop()
    options = {f"#{p.number} · {p.title}  ({p.head_ref} → {p.base_ref})": p
               for p in pulls}
    chosen = st.selectbox("Pull request", list(options.keys()))
    pr = options[chosen]
    st.caption(f"by @{pr.author} · head `{pr.head_sha[:7]}` in `{pr.head_repo}`")
    if st.button("▶ Run review", type="primary"):
        head_repo, head_ref = pr.head_repo, pr.head_sha
        pr_label = f"{repo}#{pr.number}"
        with st.spinner("Fetching diff from GitHub..."):
            target_diff = ss.gh.get_pr_diff(repo, pr.number)

else:
    try:
        branches = ss.gh.list_branches(repo)
    except GitHubError as e:
        st.error(str(e))
        branches = []
    c1, c2 = st.columns(2)
    base_br = c1.selectbox("Base branch", branches)
    head_br = c2.selectbox("Head branch (changes)", branches,
                           index=min(1, len(branches) - 1) if branches else 0)
    if st.button("▶ Run review", type="primary"):
        head_repo, head_ref = repo, head_br
        pr_label = f"{repo} {base_br}...{head_br}"
        with st.spinner("Fetching diff..."):
            target_diff = ss.gh.compare_diff(repo, base_br, head_br)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline
# ─────────────────────────────────────────────────────────────────────────────
if target_diff is not None:
    if not target_diff.strip():
        st.warning("Empty diff — nothing to review.")
        st.stop()

    try:
        from pr_review.blast import blast_radius
        from pr_review.context import build_dossier
        from pr_review.diff import map_changes, parse_diff
        from pr_review.graph import build_graph

        # ── 1. download + build graph (cached per head sha) ──────────────────
        cache = ss.graph_cache
        if head_ref in cache:
            cg, embed_idx = cache[head_ref]
            st.info(f"Using cached graph for `{head_ref[:7]}`.")
        else:
            with st.spinner("Downloading repo at PR head (tarball)..."):
                src_path = ss.gh.download_source(head_repo, head_ref)

            with st.spinner("Building multi-language code knowledge graph..."):
                cg = build_graph(src_path)

            embed_idx = None
            if use_embeddings and not dossier_only:
                with st.spinner("Building embedding index (semantic search)..."):
                    try:
                        from pr_review.embeddings import build_index
                        embed_idx = build_index(cg)
                    except Exception as e:
                        st.warning(f"Embedding index failed: {e}")

            # optional Neo4j push
            if neo4j_uri and neo4j_pass and not dossier_only:
                try:
                    from pr_review.neo4j_store import Neo4jStore
                    store = Neo4jStore(neo4j_uri, neo4j_user, neo4j_pass)
                    with st.spinner("Persisting graph to Neo4j..."):
                        store.push(cg, pr_ref=head_ref or "HEAD")
                        store.close()
                    st.success("Graph persisted to Neo4j.")
                except Exception as e:
                    st.warning(f"Neo4j push failed: {e}")

            cache[head_ref] = (cg, embed_idx)

        # ── 2. diff parsing + blast radius ────────────────────────────────────
        with st.spinner("Parsing diff and computing blast radius..."):
            file_diffs = parse_diff(target_diff)
            changes = map_changes(cg, file_diffs)
            blast = blast_radius(cg, changes)

        # graph stats
        lang_str = "  ·  ".join(f"{lang}: {cnt}"
                                  for lang, cnt in sorted(cg.lang_counts.items()))
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Graph nodes", cg.g.number_of_nodes())
        col2.metric("Graph edges", cg.g.number_of_edges())
        col3.metric("Changed nodes", len(changes))
        col4.metric("Impacted callers", int(blast.metrics.get("impacted_callers", 0)))
        if lang_str:
            st.caption(f"Languages: {lang_str}")

        # files touched
        touched_files = sorted({ch.file_path for ch in changes})
        if not changes:
            st.warning(
                "No changed nodes mapped to the graph. "
                "The diff may only touch non-source files, configs, or whitespace."
            )

        # ── 3. dossier-only mode ─────────────────────────────────────────────
        if dossier_only:
            combined = build_dossier(cg, changes, blast, target_diff,
                                     embed_index=embed_idx,
                                     token_budget=token_budget * max(len(touched_files), 1))
            ss.dossier = combined
            ss.overall = None
            ss.report_md = ""
            with st.expander("Context dossier", expanded=True):
                st.code(combined, language="markdown")
            st.stop()

        # ── 4. full review ────────────────────────────────────────────────────
        # build agent list from checkboxes
        from pr_review.agents import (
            ALL_AGENTS, SecurityAgent, CorrectnessAgent,
            PerformanceAgent, ApiContractAgent, TestCoverageAgent, ArchitectureAgent,
        )
        agent_map = {
            SecurityAgent: run_security,
            CorrectnessAgent: run_correctness,
            PerformanceAgent: run_performance,
            ApiContractAgent: run_api,
            TestCoverageAgent: run_tests,
            ArchitectureAgent: run_arch,
        }
        active_agents = [cls() for cls, enabled in agent_map.items() if enabled]
        if not active_agents:
            st.warning("No agents selected. Enable at least one in the sidebar.")
            st.stop()

        from pr_review.llm import NovaClient
        nova = NovaClient(
            model_id=model_id, region=region, max_tokens=max_output,
            aws_access_key_id=aws_key, aws_secret_access_key=aws_secret,
            aws_session_token=aws_token,
        )

        from pr_review.review import run_review, format_report

        progress = st.progress(0.0, text="Running specialist agents...")
        n_files = max(len(touched_files), 1)

        with st.spinner(f"Reviewing {n_files} file(s) with "
                        f"{len(active_agents)} specialist agent(s)..."):
            overall = run_review(
                cg=cg,
                changes=changes,
                blast=blast,
                diff_text=target_diff,
                file_diffs=file_diffs,
                nova=nova,
                embed_index=embed_idx,
                token_budget=token_budget,
                verify=verify,
                agents=active_agents,
            )
        progress.progress(1.0, text="Done.")

        ss.overall = overall
        ss.report_md = format_report(overall)
        ss.dossier = build_dossier(cg, changes, blast, target_diff,
                                   embed_index=embed_idx, token_budget=20000)

    except GitHubError as e:
        st.error(f"GitHub error: {e}")
    except Exception as e:
        import traceback
        st.error(f"{type(e).__name__}: {e}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc())


# ─────────────────────────────────────────────────────────────────────────────
# Results rendering
# ─────────────────────────────────────────────────────────────────────────────
if ss.overall is not None:
    overall = ss.overall

    # ── overall risk banner ───────────────────────────────────────────────────
    risk_color = RISK_COLORS.get(overall.risk_level, "#5F5E5A")
    st.markdown(
        f"<h2>Overall risk: "
        f"<span style='color:{risk_color}'>{overall.risk_level.upper()} "
        f"({overall.risk_score}/100)</span></h2>",
        unsafe_allow_html=True,
    )
    m = overall.metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total findings", len(overall.all_findings))
    c2.metric("Verifier dropped", overall.dropped_total)
    c3.metric("Sensitive changes", int(m.get("sensitive_changes", 0)))
    c4.metric("No-test changes", int(m.get("changes_without_tests", 0)))

    if not overall.all_findings:
        st.success("✅ No issues found across all changed files.")

    # ── per-file tabs ─────────────────────────────────────────────────────────
    files_with_findings = {fp for fp, fr in overall.file_results.items() if fr.findings}
    files_clean = {fp for fp, fr in overall.file_results.items() if not fr.findings}

    if files_with_findings:
        st.subheader("Findings by file")
        tab_paths = sorted(files_with_findings)
        tab_labels = []
        for fp in tab_paths:
            fr = overall.file_results[fp]
            badge = SEV_BADGE.get(fr.risk_level, "⚪")
            fn = fp.split("/")[-1]
            n = len(fr.findings)
            nc = fr.num_chunks
            tab_labels.append(f"{badge} {fn} ({n} finding{'s' if n != 1 else ''}, {nc} chunk{'s' if nc != 1 else ''})")

        tabs = st.tabs(tab_labels)
        for tab, fpath in zip(tabs, tab_paths):
            fr = overall.file_results[fpath]
            with tab:
                # file-level header
                color = RISK_COLORS.get(fr.risk_level, "#5F5E5A")
                st.markdown(
                    f"**`{fpath}`** — risk "
                    f"<span style='color:{color}'><b>{fr.risk_level.upper()} "
                    f"({fr.risk_score}/100)</b></span> "
                    f"— {fr.num_chunks} chunk(s) reviewed independently",
                    unsafe_allow_html=True,
                )
                st.caption(
                    f"~{fr.dossier_tokens} tokens total · "
                    f"verifier dropped {fr.dropped} finding(s)"
                )

                # ── chunk breakdown (show which lines each chunk covered) ──────
                if fr.chunk_results:
                    with st.expander(f"Chunk breakdown ({fr.num_chunks} chunk(s))", expanded=False):
                        for cr in fr.chunk_results:
                            ch = cr.chunk
                            lines_str = (
                                f"{ch.added_lines[0]}–{ch.added_lines[-1]}"
                                if len(ch.added_lines) > 1
                                else str(ch.added_lines[0])
                            )
                            nodes_str = ", ".join(
                                n.split("::")[-1] for n in ch.node_ids
                            ) or "no mapped nodes"
                            n_raw = len(cr.findings)
                            st.markdown(
                                f"**Chunk {ch.chunk_index + 1}/{ch.total_chunks}** — "
                                f"lines `{lines_str}` — `{nodes_str}` — "
                                f"{n_raw} raw finding(s) before dedup/verify"
                            )
                            if ch.dossier:
                                with st.expander(
                                    f"  Dossier for chunk {ch.chunk_index + 1} "
                                    f"(~{len(ch.dossier) // 4} tokens)",
                                    expanded=False,
                                ):
                                    st.code(ch.dossier, language="markdown")

                st.divider()

                # ── findings ─────────────────────────────────────────────────
                if not fr.findings:
                    st.success("No issues found in this file.")
                else:
                    st.markdown(f"**{len(fr.findings)} finding(s) after dedup + verify:**")

                for i, finding in enumerate(fr.findings, 1):
                    sev = finding.severity.upper()
                    badge = SEV_BADGE.get(finding.severity, "⚪")
                    label = f"{badge} {sev} · line {finding.line} · {finding.title}"
                    with st.expander(label, expanded=(finding.severity in ("critical", "high"))):
                        col_a, col_b = st.columns([2, 1])
                        with col_a:
                            st.markdown(f"**Category:** `{finding.category}`")
                            st.markdown(f"**Location:** `{finding.file}:{finding.line}`")
                        with col_b:
                            st.markdown(f"**Severity:** `{sev}`")

                        st.markdown("**What's wrong:**")
                        st.write(finding.explanation)

                        if finding.evidence:
                            st.markdown("**Evidence:**")
                            st.code(finding.evidence, language="python")

                        if finding.recommendation:
                            st.markdown("**How to fix:**")
                            st.info(finding.recommendation)

    if files_clean:
        with st.expander(f"✅ Clean files ({len(files_clean)}) — no issues found"):
            for fp in sorted(files_clean):
                st.markdown(f"- `{fp}`")

    # ── download + dossier ────────────────────────────────────────────────────
    st.divider()
    col_dl, col_dos = st.columns(2)
    with col_dl:
        st.download_button(
            "⬇ Download full report (markdown)",
            ss.report_md,
            file_name="pr_review.md",
            mime="text/markdown",
        )
    with col_dos:
        if st.button("Show combined context dossier"):
            ss["show_dossier"] = not ss.get("show_dossier", False)

    if ss.get("show_dossier") and ss.dossier:
        st.code(ss.dossier, language="markdown")
