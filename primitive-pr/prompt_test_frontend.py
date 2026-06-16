"""Streamlit test frontend for prompt generation from changed and dependent functions.

Run:
    streamlit run prompt_test_frontend.py
"""

from __future__ import annotations

import os
import json
from typing import Dict, List

import streamlit as st

from pr_review.github_client import GitHubClient, GitHubError
from pr_review.findings import SEV_BADGE, severity_counts
from pr_review.graph import build_graph
from pr_review.prompt_builder import build_changed_file_chains, build_prompts_by_function
from pr_review.review_llm import make_completion_fn
from pr_review.two_agent_review import run_two_agent_review

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


st.set_page_config(page_title="Prompt Test Frontend", layout="wide")

ss = st.session_state
ss.setdefault("gh", None)
ss.setdefault("repos", [])
ss.setdefault("user", "")
ss.setdefault("src_path", "")
ss.setdefault("prompts", {})
ss.setdefault("file_chains", {})
ss.setdefault("diff_raw", "")
ss.setdefault("target_ref", "")
ss.setdefault("backend", "primitive")
ss.setdefault("review_results", [])


def _parse_diff_files(diff_text: str) -> List[Dict]:
    out: List[Dict] = []
    cur = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if cur:
                out.append(cur)
            cur = {"path": "", "lines": []}
            continue
        if cur is None:
            continue
        if line.startswith("+++ "):
            p = line[4:].strip()
            if p != "/dev/null":
                cur["path"] = p[2:] if p.startswith("b/") else p
        elif line.startswith("@@"):
            cur["lines"].append(line)
        elif line.startswith("+") and not line.startswith("+++"):
            cur["lines"].append(line)
        elif line.startswith("-") and not line.startswith("---"):
            cur["lines"].append(line)
    if cur:
        out.append(cur)
    return [x for x in out if x["path"]]


def _connect_github(token: str) -> None:
    gh = GitHubClient(token)
    ss.gh = gh
    ss.user = gh.whoami()
    ss.repos = gh.list_repos()


with st.sidebar:
    st.header("GitHub")
    token = st.text_input(
        "Personal access token",
        type="password",
        value=os.environ.get("GITHUB_TOKEN", ""),
    )
    if st.button("Connect", use_container_width=True):
        try:
            _connect_github(token)
            st.success(f"Connected as {ss.user}")
        except GitHubError as exc:
            ss.gh = None
            st.error(str(exc))

    st.divider()
    st.header("Graph")
    ss.backend = st.selectbox("Build backend", ["vendored", "primitive", "auto"],
                              index=["vendored", "primitive", "auto"].index(ss.backend))
    depth = st.slider("Dependent traversal depth", 1, 5, 2)

    st.divider()
    st.header("Nova Review")
    nova_region = st.text_input("AWS region", value=os.environ.get("AWS_REGION", "us-east-1"))
    nova_model = st.text_input("Model ID", value=os.environ.get("NOVA_MODEL_ID", "us.amazon.nova-pro-v1:0"))
    nova_max_tokens = st.number_input("Max tokens", min_value=256, max_value=32768, value=4096, step=256)
    nova_workers = st.slider("Parallel workers", min_value=1, max_value=8, value=4)
    aws_access_key_id = st.text_input("AWS access key (optional)", value=os.environ.get("AWS_ACCESS_KEY_ID", ""))
    aws_secret_access_key = st.text_input(
        "AWS secret key (optional)",
        type="password",
        value=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
    )
    aws_session_token = st.text_input(
        "AWS session token (optional)",
        type="password",
        value=os.environ.get("AWS_SESSION_TOKEN", ""),
    )


st.title("Prompt Test Frontend")
st.caption("Builds per-changed-function prompts plus a separate per-function dependency chain output.")

if not ss.gh:
    st.info("Connect to GitHub in the sidebar.")
    st.stop()

repo = st.selectbox("Repository", ss.repos)
mode = st.radio("Target", ["Pull Request", "Commit"], horizontal=True)

pr = None
commit = None
if mode == "Pull Request":
    pulls = ss.gh.list_pulls(repo)
    if not pulls:
        st.warning("No pull requests found.")
    else:
        options = {f"#{p.number} - {p.title}": p for p in pulls}
        pr = options[st.selectbox("Pull request", list(options.keys()))]
else:
    branches = ss.gh.list_branches(repo)
    branch = st.selectbox("Branch", branches) if branches else None
    commits = ss.gh.list_commits(repo, branch) if branch else []
    if not commits:
        st.warning("No commits found.")
    else:
        commit = st.selectbox(
            "Commit",
            commits,
            format_func=lambda c: f"{c.sha[:7]} {c.date} {c.author}: {c.message}",
        )

can_run = (mode == "Pull Request" and pr is not None) or (mode == "Commit" and commit is not None)

if st.button("Build Prompts", type="primary", disabled=not can_run):
    try:
        with st.status("Building prompts", expanded=True) as status:
            if mode == "Pull Request":
                target_ref = pr.head_ref
                target_repo = pr.head_repo
                status.write("Downloading PR head source...")
                src = ss.gh.download_source(target_repo, target_ref)
                status.write("Building graph...")
                cg = build_graph(src, backend=ss.backend)
                status.write("Fetching PR diff...")
                raw = ss.gh.get_pr_diff(repo, pr.number)
            else:
                target_ref = commit.sha
                target_repo = repo
                status.write("Downloading commit source...")
                src = ss.gh.download_source(target_repo, target_ref)
                status.write("Building graph...")
                cg = build_graph(src, backend=ss.backend)
                status.write("Fetching commit diff...")
                raw = ss.gh.get_commit_diff(repo, commit.sha)

            status.write("Building per-function prompts and chains...")
            prompts = build_prompts_by_function(cg, src, raw, depth=depth)
            chains = build_changed_file_chains(cg, raw, depth=depth)

            ss.src_path = src
            ss.diff_raw = raw
            ss.prompts = prompts
            ss.file_chains = chains
            ss.target_ref = target_ref
            if not prompts:
                status.write("No prompts were generated from changed lines. Check diff overview and graph backend.")
            status.update(label="Prompt build complete", state="complete")
    except Exception as exc:
        st.error(str(exc))

can_review = bool(ss.src_path and ss.diff_raw)
if st.button("Run Review", disabled=not can_review):
    try:
        with st.status("Running two-agent Nova review", expanded=True) as status:
            status.write("Rebuilding graph context...")
            cg = build_graph(ss.src_path, backend=ss.backend)

            status.write("Creating Nova completion function...")
            complete = make_completion_fn(
                "nova",
                model_id=nova_model,
                region=nova_region,
                max_tokens=int(nova_max_tokens),
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                aws_session_token=aws_session_token,
            )

            status.write("Running Agent 1 + Agent 2 in parallel...")
            reviews = run_two_agent_review(
                cg,
                ss.src_path,
                ss.diff_raw,
                complete,
                depth=depth,
                max_workers=int(nova_workers),
            )
            ss.review_results = reviews
            status.update(label="Two-agent review complete", state="complete")
    except Exception as exc:
        st.error(f"Review failed: {exc}")

if ss.diff_raw:
    with st.expander("Diff overview"):
        files = _parse_diff_files(ss.diff_raw)
        st.caption(f"{len(files)} changed files")
        for f in files:
            with st.expander(f["path"]):
                st.code("\n".join(f["lines"]), language="diff")

if ss.prompts:
    st.subheader("Per-function LLM prompts")
    all_text: List[str] = []
    for prompt_id in sorted(ss.prompts.keys()):
        prompt = ss.prompts[prompt_id]
        all_text.append(f"## {prompt_id}\n\n{prompt}\n")
        with st.expander(prompt_id, expanded=False):
            st.text_area("Prompt", value=prompt, height=420,
                         key=f"prompt_{prompt_id}", label_visibility="collapsed")

    st.download_button(
        "Download all prompts",
        data="\n\n".join(all_text),
        file_name="per_function_prompts.md",
        mime="text/markdown",
    )
else:
    st.caption("No prompts built yet.")
    if ss.diff_raw:
        files = _parse_diff_files(ss.diff_raw)
        st.warning(
            f"Prompt generation returned 0 items for {len(files)} changed file(s). "
            "This usually means changed lines could not be mapped to function/method nodes."
        )

if ss.file_chains:
    st.subheader("Per-function dependency chains")
    chain_md: List[str] = []
    for fn_key in sorted(ss.file_chains.keys()):
        entry = ss.file_chains[fn_key]
        changed_file = entry.get("changed_file", "")
        changed_fn = entry.get("changed_function", "")
        paths = entry.get("chain_paths", [])
        deps = entry.get("dependent_files", [])
        chain_md.append(f"## {fn_key}")
        chain_md.append("")
        chain_md.append(f"Changed file: {changed_file}")
        chain_md.append(f"Changed function: {changed_fn}")
        chain_md.append("")
        chain_md.append("Dependency chains:")
        if paths:
            for p in paths:
                chain_md.append(f"- {p}")
        else:
            chain_md.append("- (none)")
        chain_md.append("")
        chain_md.append("Dependent files:")
        if deps:
            for dep in deps:
                chain_md.append(f"- {dep}")
        else:
            chain_md.append("- (none)")
        chain_md.append("")

        with st.expander(fn_key, expanded=False):
            st.markdown(f"**Changed file:** {changed_file}")
            st.markdown(f"**Changed function:** {changed_fn}")
            st.markdown("**Dependency chains**")
            st.write(paths or ["(none)"])
            st.markdown("**Dependent files**")
            st.write(deps or ["(none)"])

    st.download_button(
        "Download file chains (markdown)",
        data="\n".join(chain_md),
        file_name="changed_file_chains.md",
        mime="text/markdown",
    )
    st.download_button(
        "Download file chains (json)",
        data=json.dumps(ss.file_chains, indent=2),
        file_name="changed_file_chains.json",
        mime="application/json",
    )

if ss.review_results:
    st.subheader("Two-agent review findings (per changed file)")

    def _render_findings_block(title: str, findings):
        st.markdown(title)
        if not findings:
            st.caption("No findings.")
            return
        for f in findings:
            badge = SEV_BADGE.get(f.severity, "⚪")
            st.markdown(f"{badge} **{f.title}**")
            st.markdown(f"- Severity: `{f.severity}`")
            st.markdown(f"- Category: `{f.category}`")
            st.markdown(f"- Lines: `{f.file}:{f.line}`")
            if f.evidence:
                st.markdown("- Evidence:")
                st.code(f.evidence)
            if f.explanation:
                st.markdown(f"- Reason: {f.explanation}")
            if getattr(f, "impact_reason", ""):
                st.markdown(f"- Impact mapping: {f.impact_reason}")
            if f.recommendation:
                st.markdown(f"- Fix: {f.recommendation}")
            if getattr(f, "source_fix_example", ""):
                st.markdown(f"- Source fix example: {f.source_fix_example}")
            if getattr(f, "dependent_fix_example", ""):
                st.markdown(f"- Dependent fix example: {f.dependent_fix_example}")

    download_payload = []
    for fr in ss.review_results:
        combined = list(fr.file_findings) + list(fr.dependency_findings)
        counts = severity_counts(combined)
        summary = ", ".join(
            f"{k}:{v}" for k, v in counts.items() if v
        ) or "no findings"

        dep_by_fn = getattr(fr, "dependency_findings_by_function", {}) or {}
        if not dep_by_fn and fr.dependency_findings:
            # Backward-compat fallback if backend grouping is absent.
            for f in fr.dependency_findings:
                fn_name = str(getattr(f, "changed_function", "") or "(unknown changed function)")
                dep_by_fn.setdefault(fn_name, []).append(f)

        file_chain_entries = [
            entry for entry in ss.file_chains.values()
            if str(entry.get("changed_file", "")) == fr.path
        ]
        graph_dependee_files = sorted({
            dep
            for entry in file_chain_entries
            for dep in entry.get("dependent_files", [])
            if str(dep).strip()
        })

        with st.expander(f"{fr.path} — {summary}", expanded=False):
            if fr.errors:
                st.error("; ".join(fr.errors))

            st.markdown("### 🔎 Agent 2 dependee summary")
            st.markdown("**Dependee files (graph-based, non-LLM):**")
            st.write(graph_dependee_files or ["(none)"])

            if dep_by_fn:
                st.markdown("**Immediate dependents and breaking callsites (grouped by changed function):**")
                for fn_name in sorted(dep_by_fn.keys()):
                    fn_findings = dep_by_fn[fn_name]
                    st.markdown(f"#### Changed function: `{fn_name}`")

                    confirmed = [
                        f for f in fn_findings
                        if str(getattr(f, "provenance_status", "")) != "unverified_llm_claim"
                    ]
                    unverified = [
                        f for f in fn_findings
                        if str(getattr(f, "provenance_status", "")) == "unverified_llm_claim"
                    ]

                    if confirmed:
                        rows = []
                        for f in confirmed:
                            dep_fn = str(getattr(f, "dependent_function", "") or "(dependent unknown)")
                            dep_file = str(getattr(f, "dependent_file", "") or getattr(f, "file", "") or "")
                            dep_line = int(getattr(f, "dependent_line", 0) or 0)
                            call_file = str(getattr(f, "callsite_file", "") or getattr(f, "file", "") or "")
                            call_line = int(getattr(f, "callsite_line", 0) or getattr(f, "line", 0) or 0)
                            chain_line = str(getattr(f, "chain_line_non_llm", "") or "")
                            if not chain_line.strip() and dep_file:
                                chain_line = f"{fr.path} -> {dep_file}"
                            rows.append({
                                "severity": f.severity,
                                "category": f.category,
                                "status": str(getattr(f, "provenance_status", "") or "unknown"),
                                "changed_function": str(getattr(f, "changed_function", "") or fn_name),
                                "dependent_function": dep_fn,
                                "dependent_location": f"{dep_file}:{dep_line}" if dep_line else dep_file,
                                "callsite": f"{call_file}:{call_line}" if call_line else call_file,
                                "chain_non_llm": chain_line or "(no chain)",
                                "impact_reason": str(getattr(f, "impact_reason", "") or f.explanation or ""),
                                "source_fix_example": str(getattr(f, "source_fix_example", "") or ""),
                                "dependent_fix_example": str(getattr(f, "dependent_fix_example", "") or ""),
                            })
                        st.dataframe(rows, use_container_width=True)
                    else:
                        st.caption("No confirmed dependent callsite findings.")

                    if unverified:
                        st.warning("Unverified LLM dependency claims (outside graph-known dependents):")
                        for f in unverified:
                            st.markdown(f"- `{f.file}:{f.line}` — {f.title}")
            else:
                st.caption("No Agent 2 dependency findings grouped by changed function.")

            _render_findings_block("### 🗂 File review (Agent 1)", fr.file_findings)
            _render_findings_block("### 🔗 Dependency review (Agent 2)", fr.dependency_findings)

        download_payload.append(
            {
                "path": fr.path,
                "errors": fr.errors,
                "file_findings": [f.__dict__ for f in fr.file_findings],
                "dependency_findings": [f.__dict__ for f in fr.dependency_findings],
                "dependency_findings_by_function": {
                    k: [f.__dict__ for f in v]
                    for k, v in dep_by_fn.items()
                },
            }
        )

    st.download_button(
        "Download review findings (json)",
        data=json.dumps(download_payload, indent=2),
        file_name="two_agent_review_findings.json",
        mime="application/json",
    )
