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

    from pr_review.profiles import QUICK, STANDARD, DEEP

    depth_choices = {QUICK.label: QUICK, STANDARD.label: STANDARD, DEEP.label: DEEP}
    depth_label = st.radio(
        "Review depth",
        list(depth_choices.keys()),
        index=1,  # default: Standard
    )
    profile = depth_choices[depth_label]
    st.caption(profile.description)

    dossier_only = st.toggle(
        "Dossier only (no LLM)",
        value=False,
        help="Build context without calling Nova — no AWS needed.",
    )

    with st.expander("Model settings"):
        model_id = st.text_input("Nova model id", value="us.amazon.nova-pro-v1:0")
        region = st.text_input("AWS region", value="us-east-1")
        token_budget = st.slider(
            "Max tokens per chunk", 4000, 24000, 12000, step=500,
            help="Whole-file review: a file under this size is sent in one chunk; "
                 "larger files split on function boundaries. Keep it well under the "
                 "model's context window so a small model stays grounded.",
        )
        max_output = st.slider("Max output tokens", 1024, 5000, 4096, step=256)

    with st.expander("Advanced override"):
        st.caption(
            "By default the selected depth decides which agents, verifier, and "
            "embeddings run. Enable this to override them manually."
        )
        override = st.toggle("Override depth settings", value=False)
        run_security = st.checkbox("Security", value="security" in profile.agent_keys)
        run_correctness = st.checkbox("Correctness / regression",
                                      value="correctness" in profile.agent_keys)
        run_performance = st.checkbox("Performance",
                                      value="performance" in profile.agent_keys)
        run_api = st.checkbox("API & DB contracts",
                              value="api_contract" in profile.agent_keys)
        run_tests = st.checkbox("Test coverage",
                                value="test_coverage" in profile.agent_keys)
        run_arch = st.checkbox("Architecture",
                               value="architecture" in profile.agent_keys)
        verify = st.toggle("Evidence verifier pass", value=profile.verify,
                           help="Second pass that drops unsupported findings.")
        use_embeddings = st.toggle(
            "Embedding index (semantic search)",
            value=profile.use_embeddings,
            help="Builds a local embedding index for semantic context retrieval. "
                 "Requires sentence-transformers.",
        )

    review_config = st.toggle(
        "Also review config/lock files",
        value=False,
        help="By default, lock files (package-lock.json…), dependency manifests "
             "(requirements.txt, package.json…), and binary/generated files are skipped. "
             "Enable to review config/manifest files too. Binary/lock files stay skipped.",
    )

    # Effective settings: profile drives everything unless overridden.
    want_embeddings = use_embeddings if override else profile.use_embeddings

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
            if want_embeddings and not dossier_only:
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
        # Profile drives agents/verify; the Advanced override can replace them.
        active_agents = None
        verify_eff = profile.verify
        if override:
            from pr_review.agents import (
                SecurityAgent, CorrectnessAgent, PerformanceAgent,
                ApiContractAgent, TestCoverageAgent, ArchitectureAgent,
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
            verify_eff = verify
            if not active_agents:
                st.warning("Override is on but no agents selected. Enable at least one.")
                st.stop()

        from pr_review.llm import NovaClient
        nova = NovaClient(
            model_id=model_id, region=region, max_tokens=max_output,
            aws_access_key_id=aws_key, aws_secret_access_key=aws_secret,
            aws_session_token=aws_token,
        )

        from pr_review.review import run_review, format_report

        progress = st.progress(0.0, text="Running specialist agents...")

        def _progress(kind, i, n, path):
            if kind == "file":
                frac = min(0.9, i / max(n, 1))
                progress.progress(frac, text=f"Reviewing file {i + 1}/{n}: {path}")
            elif kind == "dependency":
                progress.progress(0.95, text="Checking cross-file dependencies...")

        with st.spinner(f"Reviewing at **{profile.key}** depth..."):
            overall = run_review(
                cg=cg,
                changes=changes,
                blast=blast,
                diff_text=target_diff,
                file_diffs=file_diffs,
                nova=nova,
                embed_index=embed_idx,
                token_budget=token_budget,
                profile=profile,
                agents=active_agents,
                verify=verify_eff if override else None,
                progress_cb=_progress,
                review_config=review_config,
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

    # ── summary dashboard (counts by severity, not a risk score) ──────────────
    SEV_ORDER = ["critical", "high", "medium", "low", "info"]

    def _sev_counts(findings):
        counts = {s: 0 for s in SEV_ORDER}
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return counts

    # in-file issues exclude breaking (breaking shown as its own tile/section)
    file_issue_findings = [
        f for fr in overall.file_results.values() for f in fr.findings
        if f.kind == "issue"
    ]
    sev = _sev_counts(file_issue_findings)

    st.subheader("Summary")
    st.caption(f"Review depth: **{overall.profile_key.upper()}**")
    cols = st.columns(6)
    cols[0].metric("🔴 Critical", sev["critical"])
    cols[1].metric("🟠 High", sev["high"])
    cols[2].metric("🟡 Medium", sev["medium"])
    cols[3].metric("🔵 Low", sev["low"])
    cols[4].metric("💥 Breaking", len(overall.breaking))
    cols[5].metric("💡 Suggestions", len(overall.suggestions))

    # ── top-level agent error banner ──────────────────────────────────────────
    all_agent_runs = [
        r
        for fr in overall.file_results.values()
        for cr in fr.chunk_results
        for r in cr.agent_runs
    ]
    agent_errors = [r for r in all_agent_runs if ":ERROR:" in r]
    if agent_errors:
        st.warning(
            f"⚠ **{len(agent_errors)} agent run(s) failed** — findings may be incomplete. "
            f"Check chunk breakdowns below for details."
        )
        with st.expander("Agent error details"):
            for err in agent_errors:
                st.error(err)

    # ── breaking-change banner (cross-file, Standard/Deep) ────────────────────
    if overall.breaking:
        st.error(
            f"⚠️ **{len(overall.breaking)} breaking change(s)** — these calls in other "
            f"files were not updated and will break."
        )
        for f in overall.breaking:
            with st.expander(
                f"💥 {f.severity.upper()} · `{f.file}:{f.line}` · {f.title}",
                expanded=True,
            ):
                st.write(f.explanation)
                if f.evidence:
                    st.code(f.evidence, language="python")
                if f.recommendation:
                    st.info(f.recommendation)

    # ── dependent callers checked (Standard/Deep) ────────────────────────────
    if overall.dependencies:
        n_callers = sum(len(dv.callers) for dv in overall.dependencies)
        st.subheader(f"🔗 Dependent callers checked ({n_callers} in other files)")
        st.caption(
            "Functions in OTHER files that call the code you changed. These were "
            "checked for breakage (Standard/Deep). 💥 = flagged as broken."
        )
        by_changed_file = {}
        for dv in overall.dependencies:
            by_changed_file.setdefault(dv.changed_file, []).append(dv)
        for cfile in sorted(by_changed_file):
            st.markdown(f"**Changed in `{cfile}`:**")
            for dv in by_changed_file[cfile]:
                label = (f"`{dv.changed_qualname}` ({dv.change_type}) — "
                         f"{len(dv.callers)} caller(s)")
                with st.expander(label):
                    for cr in dv.callers:
                        flag = "💥 " if cr.broken else "↳ "
                        st.markdown(f"{flag}**`{cr.qualname}`** · `{cr.file}:{cr.line}`")
                        st.code(cr.source, language="python")

    if not overall.all_findings:
        st.success("✅ No issues found across all changed files.")

    # ── per-file tabs (issues only; suggestions shown separately) ─────────────
    def _issue_count(fr) -> int:
        return sum(1 for f in fr.findings if f.kind == "issue")

    files_with_findings = {fp for fp, fr in overall.file_results.items() if _issue_count(fr)}
    files_clean = {fp for fp, fr in overall.file_results.items() if not _issue_count(fr)}

    if files_with_findings:
        st.subheader("Findings by file")
        tab_paths = sorted(files_with_findings)
        tab_labels = []
        def _top_sev(fr) -> str:
            for s in SEV_ORDER:
                if any(f.kind == "issue" and f.severity == s for f in fr.findings):
                    return s
            return "info"

        for fp in tab_paths:
            fr = overall.file_results[fp]
            badge = SEV_BADGE.get(_top_sev(fr), "⚪")
            fn = fp.split("/")[-1]
            n = _issue_count(fr)
            nc = fr.num_chunks
            tab_labels.append(f"{badge} {fn} ({n} issue{'s' if n != 1 else ''}, {nc} chunk{'s' if nc != 1 else ''})")

        tabs = st.tabs(tab_labels)
        for tab, fpath in zip(tabs, tab_paths):
            fr = overall.file_results[fpath]
            with tab:
                # file-level header (severity breakdown, not a risk score)
                fsev = _sev_counts([f for f in fr.findings if f.kind == "issue"])
                st.markdown(
                    f"**`{fpath}`** — "
                    f"🔴 {fsev['critical']}  🟠 {fsev['high']}  "
                    f"🟡 {fsev['medium']}  🔵 {fsev['low']}  "
                    f"— {fr.num_chunks} chunk(s) reviewed independently"
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
                            span_str = f"{ch.start_line}-{ch.end_line}"
                            if ch.added_lines:
                                changed_str = (
                                    f"{ch.added_lines[0]}–{ch.added_lines[-1]}"
                                    if len(ch.added_lines) > 1
                                    else str(ch.added_lines[0])
                                )
                            else:
                                changed_str = "none (context only)"
                            nodes_str = ", ".join(
                                n.split("::")[-1] for n in ch.node_ids
                            ) or "no mapped nodes"
                            n_raw = len(cr.findings)
                            st.markdown(
                                f"**Chunk {ch.chunk_index + 1}/{ch.total_chunks}** — "
                                f"file lines `{span_str}` · changed `{changed_str}` — "
                                f"`{nodes_str}` — "
                                f"{n_raw} raw finding(s) before dedup/verify"
                            )
                            if ch.dossier:
                                with st.expander(
                                    f"  Dossier for chunk {ch.chunk_index + 1} "
                                    f"(~{len(ch.dossier) // 4} tokens)",
                                    expanded=False,
                                ):
                                    st.code(ch.dossier, language="markdown")
                            # Agent run status — show errors prominently
                            errors = [r for r in cr.agent_runs if ":ERROR:" in r]
                            ok_runs = [r for r in cr.agent_runs if ":ERROR:" not in r]
                            if errors:
                                for err in errors:
                                    st.error(f"Agent error: {err}")
                            if ok_runs:
                                st.caption(f"Agents ran: {', '.join(ok_runs)}")

                st.divider()

                # ── findings (issues only) ───────────────────────────────────
                file_issues = [f for f in fr.findings if f.kind == "issue"]
                if not file_issues:
                    st.success("No issues found in this file.")
                else:
                    st.markdown(f"**{len(file_issues)} issue(s) after dedup + verify:**")

                for i, finding in enumerate(file_issues, 1):
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

    if overall.skipped_files:
        with st.expander(
            f"⏭️ Skipped files ({len(overall.skipped_files)}) — lock/binary/config, not reviewed"
        ):
            st.caption("Enable 'Also review config/lock files' in the sidebar to include manifests.")
            for fp in sorted(overall.skipped_files):
                st.markdown(f"- `{fp}`")

    # ── suggestions (improvements, every tier) ────────────────────────────────
    suggestions = overall.suggestions
    st.subheader(f"💡 Suggestions ({len(suggestions)})")
    if not suggestions:
        st.caption("No suggestions.")
    else:
        for f in suggestions:
            with st.expander(f"💡 `{f.file}:{f.line}` · {f.title}"):
                st.markdown(f"**Category:** `{f.category}`")
                st.write(f.explanation)
                if f.recommendation:
                    st.info(f.recommendation)

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
