"""PR Analyzer — graph-grounded, multi-pass LLM review of a pull request.

Pick a repo + PR, hit Analyze, and the app builds the code graph, loads the diff,
and runs a 3-pass severity review. Exploratory tools (diff, changed functions,
LLM prompts, graph explorer, architecture review) live in optional expanders.

Run:
    streamlit run graph_explorer.py

Credentials are read from environment variables when present (overridable in the
sidebar): GITHUB_TOKEN, NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD,
AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN/AWS_REGION, NOVA_MODEL_ID,
OPENAI_API_KEY/OPENAI_MODEL.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# Load .env (if present) so credentials are picked up from the environment.
try:
    from dotenv import load_dotenv
    load_dotenv(APP_ROOT / ".env")
except ImportError:
    pass

st.set_page_config(
    page_title="PR Analyzer",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded",
)

ss = st.session_state
ss.setdefault("gh", None)
ss.setdefault("user", None)
ss.setdefault("repos", [])
ss.setdefault("graph_ref", None)
ss.setdefault("src_path", None)
ss.setdefault("cg", None)
ss.setdefault("graph_stats", None)
ss.setdefault("diff_files", None)
ss.setdefault("diff_raw", None)
ss.setdefault("diff_pr_number", None)
ss.setdefault("pr_review_results", None)
ss.setdefault("impact_reviews", None)
ss.setdefault("impact_depth", 3)
ss.setdefault("impact_verify", True)
ss.setdefault("arch_digest_md", None)
ss.setdefault("arch_review_md", None)
ss.setdefault("provider_key", "nova")
ss.setdefault("llm_creds", {})
ss.setdefault("budget", 12000)

REL_OPTIONS = {
    "Dependent functions (calls this)": ("CALLS", "in"),
    "Functions this calls": ("CALLS", "out"),
    "Dependent files (imports)": ("IMPORTS", "out"),
    "Imported by": ("IMPORTS", "in"),
    "Instantiates": ("INSTANTIATES", "out"),
    "Instantiated by": ("INSTANTIATES", "in"),
    "Parent classes": ("INHERITS", "out"),
    "Subclasses (inheritors)": ("INHERITS", "in"),
    "Definitions in this file": ("DEFINES", "out"),
    "Defined in (parent file)": ("DEFINES", "in"),
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def parse_diff_files(diff_text: str) -> list[dict]:
    """Split a unified diff into per-file chunks with their raw diff lines."""
    files, cur = [], None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if cur:
                files.append(cur)
            cur = {"path": "", "lines": [line]}
            continue
        if cur is None:
            continue
        cur["lines"].append(line)
        if line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            path = line[4:].strip()
            cur["path"] = path[2:] if path.startswith("b/") else path
    if cur:
        files.append(cur)
    return [f for f in files if f["path"]]


def read_source(src_path: str, file_path: str, start_line: int, end_line: int) -> str:
    full = Path(src_path) / file_path
    if not full.exists():
        return f"# source not found: {file_path}"
    lines = full.read_text(errors="replace").splitlines()
    return "\n".join(lines[max(0, start_line - 1):end_line])


def get_store():
    """Build a Neo4jStore from sidebar inputs, or None if unconfigured."""
    uri = ss.get("neo4j_uri", "")
    user = ss.get("neo4j_user", "neo4j")
    pwd = ss.get("neo4j_pass", "")
    if not (uri and pwd):
        return None
    from pr_review.neo4j_store import Neo4jStore
    store = Neo4jStore(uri, user, pwd)
    if not store._available:
        return None
    return store


def sidebar_llm_inputs() -> None:
    """LLM provider + credentials, pre-filled from environment variables."""
    provider = st.selectbox("LLM provider", ["Nova (AWS Bedrock)", "OpenAI"],
                            key="sb_provider")
    pk = "nova" if provider.startswith("Nova") else "openai"
    creds = {"max_tokens": 4096}
    if pk == "nova":
        creds["model_id"] = st.text_input(
            "Nova model id",
            value=os.environ.get("NOVA_MODEL_ID", "us.amazon.nova-pro-v1:0"),
            key="sb_model")
        creds["region"] = st.text_input(
            "AWS region",
            value=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1",
            key="sb_region")
        creds["aws_access_key_id"] = st.text_input(
            "AWS access key id", value=os.environ.get("AWS_ACCESS_KEY_ID", ""),
            type="password", key="sb_akid")
        creds["aws_secret_access_key"] = st.text_input(
            "AWS secret access key", value=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
            type="password", key="sb_asak")
        creds["aws_session_token"] = st.text_input(
            "AWS session token (optional)", value=os.environ.get("AWS_SESSION_TOKEN", ""),
            type="password", key="sb_token")
    else:
        creds["model"] = st.text_input(
            "OpenAI model", value=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            key="sb_omodel")
        creds["api_key"] = st.text_input(
            "OpenAI API key", value=os.environ.get("OPENAI_API_KEY", ""),
            type="password", key="sb_okey")
    ss.provider_key = pk
    ss.llm_creds = creds
    ss.budget = st.slider("Char budget per LLM call", 4000, 24000, ss.budget, step=1000,
                          key="sb_budget")


# ─────────────────────────────────────────────────────────────────────────────
# Optional-tool renderers (used inside expanders below the report)
# ─────────────────────────────────────────────────────────────────────────────
def render_diff() -> None:
    st.caption(f"{len(ss.diff_files)} file(s) changed in PR #{ss.diff_pr_number}")
    for f in ss.diff_files:
        added = sum(1 for l in f["lines"] if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in f["lines"] if l.startswith("-") and not l.startswith("---"))
        with st.expander(f"`{f['path']}`  +{added} / -{removed}"):
            st.code("\n".join(f["lines"]), language="diff")


def render_changed_nodes() -> None:
    from pr_review.diff import parse_diff
    depth = st.slider("Hops", 1, 5, 1, key="cn_depth")
    store = get_store()
    if store is None:
        st.error("Neo4j is not configured.")
        return
    try:
        any_node = False
        for fd in parse_diff(ss.diff_raw):
            if not fd.added_lines:
                continue
            nodes = store.nodes_at_lines(fd.path, sorted(fd.added_lines), ss.graph_ref)
            if not nodes:
                continue
            any_node = True
            st.markdown(f"**`{fd.path}`** — {len(nodes)} changed node(s)")
            for node in nodes:
                label = (f"{node['kind']} · {node.get('qualname') or node.get('name')} "
                         f"(lines {node.get('start_line')}–{node.get('end_line')})")
                with st.expander(label):
                    callers = store.neighbors(node["id"], "CALLS", "in", depth=depth)
                    callees = store.neighbors(node["id"], "CALLS", "out", depth=depth)
                    c1, c2 = st.columns(2)
                    with c1:
                        st.markdown(f"**Dependents — calls this** ({len(callers)})")
                        st.dataframe([{"kind": r["kind"],
                                       "name": r.get("qualname") or r.get("name"),
                                       "path": r["path"]} for r in callers]
                                     or [{"": "—"}],
                                     use_container_width=True, hide_index=True)
                    with c2:
                        st.markdown(f"**Dependencies — this calls** ({len(callees)})")
                        st.dataframe([{"kind": r["kind"],
                                       "name": r.get("qualname") or r.get("name"),
                                       "path": r["path"]} for r in callees]
                                     or [{"": "—"}],
                                     use_container_width=True, hide_index=True)
        if not any_node:
            st.info("No graph nodes found for the changed lines.")
    finally:
        store.close()


def render_prompts() -> None:
    from pr_review.diff import parse_diff
    store = get_store()
    if store is None:
        st.error("Neo4j is not configured.")
        return
    try:
        for fd in parse_diff(ss.diff_raw):
            if not fd.added_lines:
                continue
            nodes = store.nodes_at_lines(fd.path, sorted(fd.added_lines), ss.graph_ref)
            if not nodes:
                continue
            parts = [f"# Review context for `{fd.path}`\n"]
            for node in nodes:
                src = read_source(ss.src_path, node["path"],
                                  node["start_line"], node["end_line"])
                parts.append(f"## Changed {node['kind']}: "
                             f"`{node.get('qualname') or node.get('name')}`\n\n```\n{src}\n```\n")
                deps = store.neighbors(node["id"], "CALLS", "in", depth=1)
                if deps:
                    parts.append("## Dependent functions (call this)\n")
                    for dep in deps:
                        dsrc = read_source(ss.src_path, dep["path"],
                                           dep.get("start_line", 0), dep.get("end_line", 0))
                        parts.append(f"### `{dep.get('qualname') or dep.get('name')}` "
                                     f"— `{dep['path']}`\n\n```\n{dsrc}\n```\n")
            with st.expander(f"`{fd.path}`"):
                st.text_area("Prompt", value="\n".join(parts), height=380,
                             key=f"prompt_{fd.path}", label_visibility="collapsed")
    finally:
        store.close()


def render_explorer() -> None:
    store = get_store()
    if store is None:
        st.error("Neo4j is not configured.")
        return
    try:
        kinds = store.list_kinds(ss.graph_ref)
        if not kinds:
            st.warning("No nodes found for this graph.")
            return
        col_kind, col_node = st.columns([1, 3])
        with col_kind:
            kind = st.selectbox("Kind", kinds, key="ex_kind")
        nodes = store.nodes_by_kind(kind, ss.graph_ref)

        def _label(nd: dict) -> str:
            if kind == "file":
                return nd.get("path") or nd.get("name") or nd["id"]
            return nd.get("qualname") or nd.get("name") or nd["id"]

        node_labels = {_label(nd): nd for nd in nodes}
        with col_node:
            node_label = st.selectbox(f"{kind.capitalize()} node",
                                      list(node_labels.keys()), key="ex_node")
        if not node_label:
            return
        selected = node_labels[node_label]

        detail = store.get_node(selected["id"]) or {}
        d1, d2, d3 = st.columns(3)
        d1.write(f"**Kind:** {detail.get('kind', '')}")
        d1.write(f"**Language:** {detail.get('lang', '') or '—'}")
        d2.write(f"**Path:** `{detail.get('path', '')}`")
        d2.write(f"**Test file:** {'yes' if detail.get('is_test') else 'no'}")
        d3.write(f"**Lines:** {detail.get('start_line', '?')}–{detail.get('end_line', '?')}")

        col_rel, col_depth = st.columns([3, 1])
        with col_rel:
            rel_label = st.selectbox("Retrieve", list(REL_OPTIONS.keys()), key="ex_rel")
        with col_depth:
            depth = st.slider("Hops", 1, 5, 1, key="ex_depth")
        if st.button("Run query", key="ex_run"):
            rel_type, direction = REL_OPTIONS[rel_label]
            results = store.neighbors(selected["id"], rel_type, direction, depth=depth)
            if not results:
                st.info("No connected nodes for this relationship.")
            else:
                st.success(f"{len(results)} connected node(s).")
                st.dataframe([{"kind": r.get("kind"),
                               "name": r.get("qualname") or r.get("name"),
                               "path": r.get("path"),
                               "lines": f"{r.get('start_line')}–{r.get('end_line')}"}
                              for r in results],
                             use_container_width=True, hide_index=True)
    finally:
        store.close()


ARCH_SYSTEM_PROMPT = (
    "You are a staff software engineer performing an architecture review of an entire "
    "repository. You are given a STRUCTURAL DIGEST from a code graph (modules, cross-module "
    "dependencies, cycles, hotspots, large classes, inheritance, entry points, dead code). "
    "You do NOT have full source — reason from the structure. Write a concise markdown "
    "review covering layering & violations, coupling/cohesion, dependency cycles, hotspots, "
    "possible dead code, and prioritized recommendations. Reference real module/node names."
)


def render_architecture() -> None:
    a1, a2 = st.columns(2)
    build_clicked = a1.button("Build digest", key="arch_build")
    run_clicked = a2.button("Run architecture review", key="arch_run")
    if build_clicked or run_clicked:
        store = get_store()
        if store is None:
            st.error("Neo4j is not configured.")
        else:
            try:
                from pr_review.architecture import build_digest, digest_to_markdown
                with st.spinner("Building structural digest…"):
                    ss.arch_digest_md = digest_to_markdown(build_digest(store, ss.graph_ref))
            finally:
                store.close()
    if ss.arch_digest_md:
        with st.expander("Structural digest (sent to the LLM)", expanded=build_clicked):
            st.markdown(ss.arch_digest_md)
    if run_clicked and ss.arch_digest_md:
        from pr_review.review_llm import run_completion
        try:
            with st.spinner("Asking the LLM for an architecture review…"):
                ss.arch_review_md = run_completion(
                    ss.provider_key, ARCH_SYSTEM_PROMPT, ss.arch_digest_md, **ss.llm_creds)
        except Exception as e:
            st.error(f"Review failed: {e}")
    if ss.arch_review_md:
        st.markdown("#### Architecture review")
        st.markdown(ss.arch_review_md)


def render_report() -> None:
    from pr_review.findings import SEV_BADGE, severity_counts, sort_by_severity
    results = ss.pr_review_results
    all_findings = [f for fs in results.values() for f in fs]
    counts = severity_counts(all_findings)
    st.markdown(
        "  ·  ".join(f"{SEV_BADGE[s]} {s}: {counts[s]}"
                     for s in ("critical", "high", "medium", "low"))
        + f"  ·  **{len(all_findings)} findings across {len(results)} files**"
    )

    report_lines = ["# PR review report\n"]
    for file_path, findings in results.items():
        worst = sort_by_severity(findings)[0].severity if findings else "—"
        title = f"{SEV_BADGE.get(worst, '')} `{file_path}` — {len(findings)} finding(s)"
        report_lines.append(f"\n## {file_path}\n")
        with st.expander(title, expanded=bool(findings)):
            if not findings:
                st.caption("No findings.")
                report_lines.append("_No findings._\n")
            for f in findings:
                st.markdown(f"**{SEV_BADGE.get(f.severity, '')} [{f.severity.upper()}] "
                            f"{f.title}**  \n_line {f.line} · {f.category}_")
                if f.explanation:
                    st.write(f.explanation)
                if f.evidence:
                    st.code(f.evidence)
                if f.recommendation:
                    st.markdown(f"**Fix:** {f.recommendation}")
                st.divider()
                report_lines.append(
                    f"- **[{f.severity.upper()}] {f.title}** (line {f.line}, {f.category})\n"
                    f"  - {f.explanation}\n  - Fix: {f.recommendation}\n")
    st.download_button("Download report (markdown)", "\n".join(report_lines),
                       file_name="pr_review_report.md")


def _role_badge(node) -> str:
    if node.changed:
        return "🟥"          # the changed source/intermediate
    if node.role == "consumer":
        if node.kind in ("route", "event"):
            return "🟪"      # unchanged entrypoint consumer
        if node.is_test:
            return "🧪"
        return "🟦"          # unchanged consumer (breakage candidate)
    return "⬜"


def _chain_md(chain) -> str:
    """Render one chain as 'source → … → consumer' with role badges + locations."""
    hops = []
    for n in chain.nodes:
        loc = f"`{n.path}:{n.start_line}`" if n.path else ""
        tag = ""
        if n.changed:
            tag = f" _(changed: {n.change_type})_"
        elif n.modified_in_pr:
            tag = " _(also touched)_"
        elif n.role == "consumer":
            tag = " _(unchanged)_"
        hops.append(f"{_role_badge(n)} **{n.qualname}** {loc}{tag}")
    line = "  →  ".join(hops)
    if chain.uncertain:
        line += "  · _uncertain link_"
    if chain.field_hits:
        line += f"  \n  &nbsp;&nbsp;⚠ _still uses removed/renamed field(s):_ " \
                f"`{', '.join(chain.field_hits)}`"
    return line


def render_impact_chains() -> None:
    from pr_review.findings import SEV_BADGE
    reviews = ss.impact_reviews
    if not reviews:
        st.info("No impact chains: the changed code has no unchanged consumers "
                "(callers) in the graph, or nothing in the diff mapped to a graph node.")
        return

    st.caption(
        "Each cluster groups co-changed functions; chains trace the change out to the "
        "first **unchanged** consumer it may break. 🟥 changed · 🟦 unchanged consumer · "
        "🟪 entrypoint · 🧪 test")

    from pr_review.impact import cluster_risk
    for idx, rev in enumerate(reviews):
        cluster, findings = rev.cluster, rev.findings
        members = ", ".join(ss.cg.node(m).get("qualname", m) for m in cluster.members
                            if ss.cg.has(m))
        n_break = sum(1 for f in findings if f.category == "breaking")
        # risk: deterministic signal, bumped to high if the LLM confirmed a break
        risk_label, risk_emoji = cluster_risk(cluster)
        if n_break:
            risk_label, risk_emoji = "high", "🔴"
        title = (f"{risk_emoji} [{risk_label.upper()}] {members} — "
                 f"{len(cluster.chains)} consumer(s)"
                 f"{f', {n_break} breaking' if n_break else ''}")
        with st.expander(title, expanded=bool(n_break)):
            st.markdown("**Propagation chains**")
            for ch in cluster.chains:
                st.markdown("- " + _chain_md(ch))
            if cluster.extra_consumers:
                st.caption(f"+{cluster.extra_consumers} more consumer(s) not shown")

            st.markdown("**LLM verdict**")
            if not findings:
                st.caption("No breakage found by the model for these chains.")
            for f in findings:
                st.markdown(f"{SEV_BADGE.get(f.severity, '')} **[{f.severity.upper()}] "
                            f"{f.title}**  \n_`{f.file}:{f.line}` · {f.category}_")
                if f.explanation:
                    st.write(f.explanation)
                if f.evidence:
                    st.code(f.evidence)
                if f.recommendation:
                    st.markdown(f"**Fix:** {f.recommendation}")
                st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("🔗 GitHub")
    pat = st.text_input("Personal access token", type="password",
                        value=os.environ.get("GITHUB_TOKEN", ""),
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
    st.header("🗄️ Neo4j")
    ss.neo4j_uri = st.text_input("URI", value=ss.get("neo4j_uri")
                                 or os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    ss.neo4j_user = st.text_input("User", value=ss.get("neo4j_user")
                                  or os.environ.get("NEO4J_USER", "neo4j"))
    ss.neo4j_pass = st.text_input("Password", type="password",
                                  value=ss.get("neo4j_pass")
                                  or os.environ.get("NEO4J_PASSWORD", ""))

    st.divider()
    st.header("🤖 LLM")
    sidebar_llm_inputs()


# ─────────────────────────────────────────────────────────────────────────────
# Main — PR Analyzer
# ─────────────────────────────────────────────────────────────────────────────
st.title("🔎 PR Analyzer")
st.caption("Graph-grounded, multi-pass LLM review of a pull request.")

if not ss.gh:
    st.info("Connect to GitHub in the sidebar to begin.")
    st.stop()

repo = st.selectbox("Repository", ss.repos)
pulls = []
if repo:
    from pr_review.github_client import GitHubError
    try:
        pulls = ss.gh.list_pulls(repo)
    except GitHubError as e:
        st.error(str(e))

if not pulls:
    st.warning("No open pull requests found for this repo.")
    st.stop()

pr_labels = {f"#{p.number} — {p.title}": p for p in pulls}
pr = pr_labels[st.selectbox("Pull request", list(pr_labels.keys()))]

col_a, col_b = st.columns([1, 3])
analyze = col_a.button("Analyze PR", type="primary", use_container_width=True)
rebuild = col_b.checkbox("Rebuild graph (ignore cache)", value=False)
ss.impact_depth = col_b.slider(
    "Impact depth (max hops a change is traced)", 1, 5, ss.impact_depth,
    help="How many call-graph hops to follow from the changed code out to the unchanged "
         "consumers it may break. Each chain stops at the first unchanged consumer; this caps "
         "how far through changed intermediates we walk to reach it.")
ss.impact_verify = col_b.checkbox(
    "Verify findings (extra precision pass)", value=ss.impact_verify,
    help="Run a second, strict LLM pass that drops impact findings not grounded in a specific "
         "consumer line. Fewer false positives, one extra call per cluster with findings.")

if analyze:
    store = get_store()
    if store is None:
        st.error("Neo4j is not configured (set URI + password in the sidebar) or the "
                 "`neo4j` driver is not installed.")
    else:
        with st.status("Analyzing PR…", expanded=True) as status:
            try:
                # 1. graph (cached per PR head unless rebuild)
                if rebuild or ss.graph_ref != pr.head_ref or not ss.src_path \
                        or ss.cg is None:
                    from pr_review.graph import build_graph
                    status.write(f"Downloading `{repo}` at head `{pr.head_ref}`…")
                    src = ss.gh.download_source(pr.head_repo, pr.head_ref)
                    status.write("Building code graph…")
                    cg = build_graph(src)
                    status.write("Pushing graph to Neo4j…")
                    store.push(cg, pr_ref=pr.head_ref)
                    ss.graph_ref = pr.head_ref
                    ss.src_path = src
                    ss.cg = cg          # kept in-memory for impact-chain analysis
                    ss.graph_stats = (cg.g.number_of_nodes(), cg.g.number_of_edges())
                else:
                    status.write("Using cached graph.")

                # 2. diff
                status.write("Loading diff…")
                raw = ss.gh.get_pr_diff(repo, pr.number)
                ss.diff_raw = raw
                ss.diff_files = parse_diff_files(raw)
                ss.diff_pr_number = pr.number

                # 3. review — track A (per-file) + track B (impact chains)
                from pr_review.diff import parse_diff
                from pr_review.pr_passes import review_pr, review_pr_impact
                from pr_review.review_llm import make_completion_fn

                complete_fn = make_completion_fn(ss.provider_key, **ss.llm_creds)
                file_diffs = parse_diff(raw)
                diff_by_file = {f["path"]: "\n".join(f["lines"]) for f in ss.diff_files}

                def _cb(i, total, path):
                    if path:
                        status.write(f"Reviewing file {i + 1}/{total}: `{path}`")

                ss.pr_review_results = review_pr(
                    ss.src_path, file_diffs, complete_fn, budget=ss.budget,
                    progress_cb=_cb, diff_by_file=diff_by_file)

                def _cb_impact(i, total, name):
                    if total and i < total:
                        status.write(f"Tracing impact {i + 1}/{total}"
                                     + (f": `{name}`" if name else ""))

                status.write("Tracing impact chains…")
                ss.impact_reviews = review_pr_impact(
                    ss.cg, ss.src_path, file_diffs, complete_fn,
                    budget=ss.budget, max_depth=ss.impact_depth,
                    diff_by_file=diff_by_file, verify=ss.impact_verify,
                    progress_cb=_cb_impact)
                status.update(label="Analysis complete ✓", state="complete", expanded=False)
            except Exception as e:
                status.update(label="Analysis failed", state="error")
                st.error(str(e))
            finally:
                store.close()

if ss.graph_stats:
    n, e = ss.graph_stats
    m1, m2, m3 = st.columns(3)
    m1.metric("Graph nodes", n)
    m2.metric("Graph edges", e)
    m3.metric("Files changed", len(ss.diff_files or []))

# ── Impact chains (primary) ──────────────────────────────────────────────────
if ss.impact_reviews is not None:
    st.divider()
    st.subheader("🌊 Impact chains — what this change breaks")
    render_impact_chains()

# ── Report (headline) ────────────────────────────────────────────────────────
if ss.pr_review_results:
    st.divider()
    st.subheader("Review report")
    render_report()

# ── Optional tools ───────────────────────────────────────────────────────────
if ss.diff_files or ss.graph_ref:
    st.divider()
    st.subheader("Tools")

    if ss.diff_files:
        with st.expander("📄 PR diff"):
            render_diff()
    if ss.graph_ref and ss.diff_raw:
        with st.expander("🔗 Changed functions & dependencies"):
            render_changed_nodes()
        with st.expander("🧩 LLM prompts per file"):
            render_prompts()
    if ss.graph_ref:
        with st.expander("🕸️ Graph explorer"):
            render_explorer()
        with st.expander("🏛️ Architecture review"):
            render_architecture()
