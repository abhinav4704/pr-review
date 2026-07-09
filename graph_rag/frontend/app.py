"""Streamlit frontend: GitHub -> Neo4j -> 2-agent code analysis.

Run (from this directory's parent, i.e. graph_rag/):
    .\\venv\\Scripts\\python.exe -m streamlit run frontend/app.py

Pipeline, each step collapsible with its own output:
  1. Connect to GitHub, pick a repo + ref, download a source snapshot.
  2. Index into Neo4j (shared structural graph).
  3. Agent B, pass 1: deterministic taint composition from graph DFG facts (no LLM).
  4. Agent A: correctness + impact, chunked over the repo (LLM).
  5. Agent B, pass 2: security discovery — reads reachable chains + taint evidence, free-form (LLM).
  6. Agent B, pass 3: architecture/layering — shape catalogue + raw-code deep-dive (LLM).
  7. Combined, deduped, scored findings + JSON download.

Identity/flow generation and embeddings-based retrieval live in the separate
`rag/` tool, not here — this app is analyzer-only.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from gh_client import GitHubClient, GitHubError  # noqa: E402

from graph_rag.graph_core.config import neo4j_config  # noqa: E402
from graph_rag.graph_core.store import GraphStore  # noqa: E402
from graph_rag.graph_core.pipeline import index_repo  # noqa: E402
from graph_rag.graph_core.llm import SemanticLLM, DEFAULT_PROVIDER, PROVIDERS, default_model  # noqa: E402
from graph_rag.graph_core.findings import Finding  # noqa: E402
from graph_rag.analyzer import agents as agents_mod  # noqa: E402
from graph_rag.analyzer import taint as taint_mod  # noqa: E402
from graph_rag.analyzer import scoring as scoring_mod  # noqa: E402
from graph_rag.analyzer.dedup import dedupe_findings  # noqa: E402

st.set_page_config(page_title="Graph RAG — 2-agent code analysis", layout="wide")

ss = st.session_state
ss.setdefault("gh", None)
ss.setdefault("gh_user", "")
ss.setdefault("repos", [])
ss.setdefault("repo_path", "")
ss.setdefault("repo_name", "")
ss.setdefault("step_results", {})   # step_key -> arbitrary result payload
ss.setdefault("step_done", {})      # step_key -> bool


def _llm() -> SemanticLLM:
    model = ss.get("model") or default_model(ss.get("provider", DEFAULT_PROVIDER))
    return SemanticLLM(provider=ss.get("provider", DEFAULT_PROVIDER), model=model)


def _mark(step: str, result) -> None:
    ss.step_results[step] = result
    ss.step_done[step] = True


def _done(step: str) -> bool:
    return bool(ss.step_done.get(step))


# ----------------------------------------------------------------- sidebar --
with st.sidebar:
    st.header("1 — GitHub")
    token = st.text_input("Personal access token", type="password",
                          value=os.environ.get("GITHUB_TOKEN", ""))
    if st.button("Connect", use_container_width=True):
        try:
            gh = GitHubClient(token)
            ss.gh = gh
            ss.gh_user = gh.whoami()
            ss.repos = gh.list_repos()
            st.success(f"Connected as {ss.gh_user}")
        except GitHubError as exc:
            ss.gh = None
            st.error(str(exc))

    st.divider()
    st.header("LLM")
    ss.provider = st.selectbox("Provider", PROVIDERS,
                               index=PROVIDERS.index(DEFAULT_PROVIDER) if DEFAULT_PROVIDER in PROVIDERS else 0)
    ss.model = st.text_input("Model id (blank = provider default)",
                             value=os.environ.get("GRAPH_RAG_LLM_MODEL", ""))
    ss.limit = st.number_input("Candidate/function cap (0 = no cap, smoke-test aid)",
                               min_value=0, value=0, step=5)


st.title("Graph RAG — 2-agent code analysis")
st.caption(
    "Agent A = correctness + impact (merged, one LLM call per chunk) · "
    "Agent B = taint composition + free-form security discovery + architecture/layering (reads raw chain source directly)"
)

limit = int(ss.limit) or None

# ------------------------------------------------------------- step 1: repo -
with st.expander("Step 1 — Select source", expanded=not ss.repo_path):
    source_mode = st.radio(
        "Source", ["GitHub repo", "Local folder path", "Upload .zip"],
        horizontal=True, key="source_mode",
    )

    if source_mode == "GitHub repo":
        if not ss.gh:
            st.info("Connect to GitHub in the sidebar to use this option.")
        else:
            st.warning(
                "TLS certificate verification is disabled for GitHub requests "
                "(per configured setting). Only use this on a trusted network.",
                icon="⚠️",
            )
            repo = st.selectbox("Repository", ss.repos)
            branches = []
            branch_error = None
            if repo:
                try:
                    branches = ss.gh.list_branches(repo)
                except GitHubError as exc:
                    branch_error = str(exc)
                    st.error(branch_error)
            default_branch = None
            if branches:
                try:
                    default_branch = ss.gh.default_branch(repo)
                except GitHubError:
                    default_branch = branches[0]

            if branches:
                ref = st.selectbox("Branch", branches,
                                   index=branches.index(default_branch) if default_branch in branches else 0)
            else:
                st.caption("No branches loaded — enter a branch/tag/SHA manually below.")
                ref = st.text_input("Branch/ref (manual)", value="main")
            repo_name = st.text_input("Repo name in Neo4j", value=repo.split("/")[-1] if repo else "",
                                      key="gh_repo_name")

            if st.button("Download source", disabled=not (repo and ref), type="primary"):
                with st.spinner(f"Downloading {repo}@{ref}..."):
                    try:
                        path = ss.gh.download_source(repo, ref)
                        ss.repo_path = path
                        ss.repo_name = repo_name
                        st.success(f"Downloaded to {path}")
                    except GitHubError as exc:
                        st.error(str(exc))

    elif source_mode == "Local folder path":
        st.caption(
            "Point at a folder already on this machine (no upload/copy — "
            "read directly from the given path)."
        )
        folder = st.text_input("Absolute folder path", value="", key="local_folder_path")
        local_name = st.text_input(
            "Repo name in Neo4j",
            value=os.path.basename(os.path.normpath(folder)) if folder else "",
            key="local_repo_name",
        )
        if st.button("Use this folder", disabled=not folder, type="primary"):
            if not os.path.isdir(folder):
                st.error(f"not a directory: {folder}")
            else:
                ss.repo_path = os.path.abspath(folder)
                ss.repo_name = local_name or os.path.basename(os.path.normpath(folder))
                st.success(f"Using {ss.repo_path}")

    else:  # Upload .zip
        st.caption(
            "Upload a .zip of the codebase. If it has a single top-level "
            "folder (typical GitHub download layout), that folder is used "
            "directly as the repo root."
        )
        uploaded = st.file_uploader("Codebase .zip", type=["zip"], key="zip_uploader")
        zip_name = st.text_input(
            "Repo name in Neo4j",
            value=os.path.splitext(uploaded.name)[0] if uploaded else "",
            key="zip_repo_name",
        )
        if st.button("Extract & use this zip", disabled=not uploaded, type="primary"):
            with st.spinner("Extracting..."):
                try:
                    dest_root = tempfile.mkdtemp(prefix="graph_rag_upload_")
                    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                        tmp.write(uploaded.getbuffer())
                        zpath = tmp.name
                    try:
                        with zipfile.ZipFile(zpath) as zf:
                            zf.extractall(dest_root)
                    except zipfile.BadZipFile:
                        st.error("not a valid .zip file")
                        raise
                    finally:
                        os.remove(zpath)

                    entries = [e for e in os.listdir(dest_root) if not e.startswith("__MACOSX")]
                    if len(entries) == 1 and os.path.isdir(os.path.join(dest_root, entries[0])):
                        root = os.path.join(dest_root, entries[0])
                    else:
                        root = dest_root
                    ss.repo_path = root
                    ss.repo_name = zip_name or "uploaded"
                    st.success(f"Extracted to {root}")
                except zipfile.BadZipFile:
                    pass

    if ss.repo_path:
        st.write(f"**repo path:** `{ss.repo_path}`")
        st.write(f"**repo name (Neo4j):** `{ss.repo_name}`")


if not ss.repo_path:
    st.stop()

repo_path = ss.repo_path
repo_name = ss.repo_name

# ------------------------------------------------------- run everything -----
st.divider()
with st.container(border=True):
    st.subheader("🚀 Run full pipeline")
    st.caption(
        "Runs steps 2-7 end to end with current sidebar/LLM settings: index -> "
        "Agent B pass 1 (taint composition) -> Agent A -> Agent B pass 2 (security discovery) -> "
        "Agent B pass 3 (architecture) -> combined + scored. "
        "Equivalent to clicking every step's button in order."
    )
    col_a, col_b = st.columns(2)
    with col_a:
        rp_wipe = st.checkbox("Wipe existing repo data first", value=True, key="rp_wipe")
    with col_b:
        rp_skip_qualify = st.checkbox("Skip Step 7 LLM qualify", value=False, key="rp_skip_qualify")

    if st.button("▶ Run pipeline (everything)", type="primary", use_container_width=True):
        log_box = st.empty()
        lines: list[str] = []

        def _log_rp(msg: str) -> None:
            lines.append(str(msg))
            log_box.code("\n".join(lines[-400:]))

        progress = st.progress(0.0, text="Starting...")
        llm = _llm()

        try:
            # 2. index
            progress.progress(1 / 6, text="Step 2 — indexing...")
            store = GraphStore(neo4j_config())
            try:
                res = index_repo(repo_path, repo_name, store, wipe=rp_wipe)
            finally:
                store.close()
            _mark("index", {
                "files": res.files, "nodes": res.nodes, "edges": res.edges,
                "seconds": round(res.seconds, 2), "db_counts": res.db_counts,
                "coverage": {k: v.__dict__ if hasattr(v, "__dict__") else v for k, v in res.coverage.items()},
                "validation": res.validation,
            })
            _log_rp(f"[index] files={res.files} nodes={res.nodes} edges={res.edges}")

            # 3. Agent B pass 1: deterministic taint composition (reads dfg_json from graph)
            progress.progress(2 / 6, text="Step 3 — Agent B pass 1 (taint composition)...")
            store = GraphStore(neo4j_config())
            try:
                taint_findings = taint_mod.find_taint_findings(
                    store, repo_name, root=repo_path, max_hops=ss.get("max_hops", 6))
            finally:
                store.close()
            _mark("taint", {"findings": taint_findings})
            _log_rp(f"[taint] deterministic findings={len(taint_findings)}")

            # 4. Agent A: correctness + impact
            progress.progress(3 / 6, text="Step 4 — Agent A (correctness + impact)...")
            store = GraphStore(neo4j_config())
            try:
                a_findings = agents_mod.run_agent_a_scan(store, repo_path, repo_name, llm,
                                                         file_limit=limit, log=_log_rp)
            finally:
                store.close()
            _mark("agent_a", a_findings)
            _log_rp(f"[agent A] findings={len(a_findings)}")

            # 5. Agent B pass 2: security discovery (free-form, taint as evidence)
            progress.progress(4 / 6, text="Step 5 — Agent B pass 2 (security discovery)...")
            store = GraphStore(neo4j_config())
            try:
                qualified = taint_mod.run_agent_b_security(
                    store, repo_name, repo_path, llm, max_hops=ss.get("max_hops", 6), limit=None)
            finally:
                store.close()
            _mark("agent_b_qualify", qualified)
            _log_rp(f"[agent B security] discovery findings={len(qualified)}")

            # 6. Agent B pass 3: architecture/layering
            progress.progress(5 / 6, text="Step 6 — Agent B pass 3 (architecture)...")
            store = GraphStore(neo4j_config())
            try:
                arch = taint_mod.run_architecture_pass(store, repo_name, repo_path, llm, shape_limit=None)
            finally:
                store.close()
            _mark("agent_b_architecture", arch)
            _log_rp(f"[agent B architecture] shapes_total={arch['shapes_total']} "
                    f"shapes_flagged={arch['shapes_flagged']} findings={len(arch['findings'])}")

            # 7. combine + score
            progress.progress(1.0, text="Step 7 — combining + scoring...")
            stage1 = [
                Finding(category="security", subcategory=f["subcategory"], source="graph_proven",
                       owning_fqn=f["owning_fqn"], file=f["file"], line=f["line"],
                       message=f["message"], confidence="HIGH")
                for f in taint_findings
            ]
            # Agent B security findings are DISCOVERY now — merge into the scored
            # set and dedup (graph_proven beats an llm re-report of the same family).
            all_findings = dedupe_findings(stage1 + a_findings + arch["findings"] + qualified)

            store = GraphStore(neo4j_config())
            try:
                scored = scoring_mod.score_findings(store, repo_name, all_findings,
                                                    llm=None if rp_skip_qualify else llm)
            finally:
                store.close()
            ss.step_results["combined"] = {
                "scored": scored,
                "agent_b_qualified": qualified,
            }
            _log_rp(f"[combined] scored findings={len(scored)}")

            progress.progress(1.0, text="Done.")
            st.success("Full pipeline complete — see each step's expander below for details.")
        except Exception as exc:
            st.error(f"Pipeline failed: {exc}")
            raise

# --------------------------------------------------------- step 2: indexing -
with st.expander("Step 2 — Index into Neo4j (shared structural graph)", expanded=not _done("index")):
    wipe = st.checkbox("Wipe existing repo data first", value=True, key="index_wipe")
    if st.button("Run indexing", type="primary"):
        with st.spinner("Indexing..."):
            store = GraphStore(neo4j_config())
            try:
                res = index_repo(repo_path, repo_name, store, wipe=wipe)
            finally:
                store.close()
            _mark("index", {
                "files": res.files, "nodes": res.nodes, "edges": res.edges,
                "seconds": round(res.seconds, 2), "db_counts": res.db_counts,
                "coverage": {k: v.__dict__ if hasattr(v, "__dict__") else v for k, v in res.coverage.items()},
                "validation": res.validation,
            })
    if _done("index"):
        st.json(ss.step_results["index"])

if not _done("index"):
    st.stop()

# ------------------------------------------------------- step 3: taint ------
with st.expander("Step 3 — Agent B pass 1: deterministic taint composition (no LLM)",
                 expanded=not _done("taint")):
    max_hops = st.slider("Max composition hops", 1, 10, 6, key="max_hops")
    if st.button("Run taint composition pass", type="primary"):
        with st.spinner("Composing source-to-sink chains from graph DFG facts..."):
            store = GraphStore(neo4j_config())
            try:
                findings = taint_mod.find_taint_findings(
                    store, repo_name, root=repo_path, max_hops=max_hops)
            finally:
                store.close()
            _mark("taint", {"findings": findings})
    if _done("taint"):
        res = ss.step_results["taint"]
        st.write(f"**deterministic (graph_proven) findings: {len(res['findings'])}**")
        for f in res["findings"]:
            st.write(f"- `[{f['subcategory']}]` {f['owning_fqn']} ({f['file']}:{f['line']}) — {f['message']}")

if not _done("taint"):
    st.stop()

# --------------------------------------------------- step 4: Agent A --------
st.header("Step 4 — Agent A: correctness + impact")

with st.expander("Agent A — correctness + impact (one LLM call per whole-function chunk)",
                 expanded=not _done("agent_a")):
    if st.button("Run Agent A", key="run_agent_a"):
        with st.spinner("Agent A scanning files..."):
            store = GraphStore(neo4j_config())
            try:
                findings = agents_mod.run_agent_a_scan(store, repo_path, repo_name, _llm(), file_limit=limit)
            finally:
                store.close()
            _mark("agent_a", findings)
    if _done("agent_a"):
        fs = ss.step_results["agent_a"]
        st.write(f"**{len(fs)} finding(s)**")
        for f in fs:
            st.write(f"- `[{f.subcategory}]` {f.owning_fqn} ({f.file}:{f.line}) — {f.message}")

# --------------------------------------------- step 5: Agent B security -----
st.header("Step 5 — Agent B pass 2: security discovery")

with st.expander("Agent B — security discovery (reads reachable chains + taint evidence, free-form)",
                 expanded=not _done("agent_b_qualify")):
    qualify_limit = st.number_input("Cap chains reviewed (cost control, 0 = no cap)",
                                    min_value=0, value=0, step=1, key="qualify_limit")
    if st.button("Run Agent B security", key="run_agent_b_qualify"):
        with st.spinner("Agent B reviewing reachable chains for vulnerabilities..."):
            store = GraphStore(neo4j_config())
            try:
                findings = taint_mod.run_agent_b_security(store, repo_name, repo_path, _llm(),
                                                          max_hops=ss.get("max_hops", 6),
                                                          limit=int(qualify_limit) or None)
            finally:
                store.close()
            _mark("agent_b_qualify", findings)
    if _done("agent_b_qualify"):
        fs = ss.step_results["agent_b_qualify"]
        st.write(f"**{len(fs)} security finding(s)**")
        for f in fs:
            st.write(f"- `[{f.subcategory}]` {f.owning_fqn} ({f.file}:{f.line}) — {f.message}")

# ------------------------------------------------ step 6: Agent B arch ------
st.header("Step 6 — Agent B pass 3: architecture/layering")

with st.expander("Agent B — architecture (shape catalogue -> flag -> raw-code deep-dive)",
                 expanded=not _done("agent_b_architecture")):
    shape_limit = st.number_input("Shape catalogue cap sent to Stage 1 (0 = no cap)",
                                  min_value=0, value=0, step=5, key="shape_limit")
    if st.button("Run Agent B architecture", key="run_agent_b_arch"):
        with st.spinner("Collecting shapes + flagging + deep-diving..."):
            store = GraphStore(neo4j_config())
            try:
                result = taint_mod.run_architecture_pass(
                    store, repo_name, repo_path, llm=_llm(),
                    shape_limit=int(shape_limit) or None,
                )
            finally:
                store.close()
            _mark("agent_b_architecture", result)
    if _done("agent_b_architecture"):
        result = ss.step_results["agent_b_architecture"]
        st.write(f"shapes_total={result.get('shapes_total')}  shapes_flagged={result.get('shapes_flagged')}")
        fs = result["findings"]
        st.write(f"**{len(fs)} finding(s)**")
        for f in fs:
            st.write(f"- `[{f.subcategory}]` {f.owning_fqn} ({f.file}:{f.line}) — {f.message}")

# ------------------------------------------------- step 7: combined output --
st.header("Step 7 — Combined findings")
with st.expander("Combined, deduped, scored findings", expanded=True):
    st.caption(
        "Qualify (LLM) checks whether each blast-radius caller passes genuinely "
        "dangerous input vs. hardcoded/guarded args — optional. Skip it to get "
        "raw blast counts with zero extra LLM calls."
    )
    skip_qualify = st.checkbox("Skip LLM qualify (raw blast counts only, no LLM calls)",
                               value=False, key="skip_qualify")
    if st.button("Combine + score all findings", type="primary"):
        with st.spinner("Scoring (blast radius + severity)..."):
            taint_det = ss.step_results.get("taint", {}).get("findings", [])
            stage1 = [
                Finding(category="security", subcategory=f["subcategory"], source="graph_proven",
                       owning_fqn=f["owning_fqn"], file=f["file"], line=f["line"],
                       message=f["message"], confidence="HIGH")
                for f in taint_det
            ]
            agent_a = ss.step_results.get("agent_a", [])
            arch = ss.step_results.get("agent_b_architecture", {}).get("findings", [])
            security = ss.step_results.get("agent_b_qualify", [])
            # Agent B security findings are DISCOVERY now — merge into scored and
            # dedup (graph_proven beats an llm re-report of the same family).
            all_findings = dedupe_findings(stage1 + agent_a + arch + security)

            store = GraphStore(neo4j_config())
            try:
                scored = scoring_mod.score_findings(store, repo_name, all_findings,
                                                    llm=None if skip_qualify else _llm())
            finally:
                store.close()

            ss.step_results["combined"] = {"scored": scored}

    combined = ss.step_results.get("combined")
    if combined:
        scored = combined["scored"]
        st.subheader(f"Scored findings (Agent A + Agent B security + architecture + deterministic taint) — {len(scored)}")
        for f in scored:
            st.write(
                f"**{f.severity_label}** {f.severity:.2f}  `[{f.category}/{f.subcategory}]` "
                f"{f.owning_fqn} ({f.file}:{f.line}) blast={f.blast_count}/{f.qualified_blast_count}"
            )
            st.caption(f.message)

        combined_json = json.dumps({
            "scored": [f.to_dict() for f in scored],
        }, indent=2, sort_keys=True)
        st.download_button("Download combined JSON", combined_json,
                           file_name=f"{repo_name}_findings.json", mime="application/json")
