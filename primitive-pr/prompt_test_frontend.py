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
from pr_review.prompt_builder import (
    build_changed_file_chains,
    build_prompts_by_function,
    build_prompts_for_selected_files,
    build_selected_file_chains,
)
from pr_review.review_llm import make_completion_fn
from pr_review.two_agent_review import run_two_agent_review, run_two_agent_review_manual
from pr_review.upload_utils import materialize_uploaded_sources

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
# Normalize legacy session values that referenced vendored/auto backend.
if ss.get("backend") not in {"primitive", None}:
    ss["backend"] = "primitive"
ss.setdefault("backend", "primitive")
ss.setdefault("review_results", [])
ss.setdefault("manual_ref", "")
ss.setdefault("manual_files", [])
ss.setdefault("manual_repo_files", [])
ss.setdefault("manual_loaded_ref", "")
ss.setdefault("review_mode", "")
ss.setdefault("manual_prompt_selected", {})
ss.setdefault("upload_src_path", "")
ss.setdefault("upload_files", [])


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


_DEFAULT_CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php",
    ".cs", ".swift", ".kt", ".kts", ".scala", ".m", ".mm", ".c", ".cc", ".cpp",
    ".h", ".hpp", ".sql", ".sh", ".bash", ".zsh", ".ps1", ".yaml", ".yml", ".toml",
    ".json", ".xml", ".proto", ".graphql",
}


def _list_repo_files(src_root: str) -> List[str]:
    out: List[str] = []
    if not src_root or not os.path.isdir(src_root):
        return out

    skip_dirs = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv"}
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for fname in filenames:
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _DEFAULT_CODE_EXTS:
                continue
            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, src_root).replace("\\", "/")
            out.append(rel_path)
    return sorted(set(out))


def _manual_diff_overview(selected_files: List[str]) -> str:
    lines: List[str] = []
    for path in selected_files:
        lines.extend(
            [
                f"diff --git a/{path} b/{path}",
                f"--- a/{path}",
                f"+++ b/{path}",
                "@@ manual-selection @@",
                "+MANUAL FILE SELECTED",
                "",
            ]
        )
    return "\n".join(lines).strip()


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
    ss.backend = st.selectbox("Build backend", ["primitive"], index=0)
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
mode = st.radio("Target", ["Pull Request", "Commit", "Manual Files", "Upload Files"], horizontal=True)

pr = None
commit = None
manual_ref = ""
if mode == "Pull Request":
    pulls = ss.gh.list_pulls(repo)
    if not pulls:
        st.warning("No pull requests found.")
    else:
        options = {f"#{p.number} - {p.title}": p for p in pulls}
        pr = options[st.selectbox("Pull request", list(options.keys()))]
elif mode == "Commit":
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
else:
    if mode == "Manual Files":
        branches = ss.gh.list_branches(repo)
        if not branches:
            st.warning("No branches found.")
        else:
            default_idx = branches.index(ss.manual_ref) if ss.manual_ref in branches else 0
            manual_ref = st.selectbox("Branch/Ref", branches, index=default_idx)
            ss.manual_ref = manual_ref

            if st.button("Load file list", key="load_manual_file_list"):
                with st.status("Loading repository files", expanded=False) as status:
                    status.write("Downloading source snapshot...")
                    src = ss.gh.download_source(repo, manual_ref)
                    files = _list_repo_files(src)
                    ss.manual_repo_files = files
                    ss.src_path = src
                    ss.target_ref = manual_ref
                    ss.manual_loaded_ref = manual_ref
                    status.update(label=f"Loaded {len(files)} files", state="complete")

            if ss.manual_loaded_ref and ss.manual_loaded_ref != manual_ref:
                st.info("Branch/ref changed. Click 'Load file list' to refresh selectable files.")

            if ss.manual_repo_files:
                st.caption("Select files by folder")
                selected_now: list[str] = []
                grouped: dict[str, list[str]] = {}
                for path in ss.manual_repo_files:
                    parts = path.split("/", 1)
                    folder = parts[0] if len(parts) > 1 else "(root)"
                    grouped.setdefault(folder, []).append(path)

                for folder in sorted(grouped.keys()):
                    files = sorted(grouped[folder])
                    with st.expander(f"{folder} ({len(files)})", expanded=False):
                        all_key = f"manual_folder_all::{folder}"
                        select_all = st.checkbox(
                            f"Select all in {folder}",
                            key=all_key,
                            value=all(f in ss.manual_files for f in files),
                        )
                        for fpath in files:
                            file_key = f"manual_file::{fpath}"
                            if select_all:
                                st.session_state[file_key] = True
                            checked = st.checkbox(
                                fpath.split("/", 1)[-1],
                                key=file_key,
                                value=fpath in ss.manual_files,
                            )
                            if checked:
                                selected_now.append(fpath)

                ss.manual_files = sorted(set(selected_now))
            else:
                st.caption("Load a branch/ref to pick files.")
    else:
        st.caption("Upload direct code files, a ZIP file, or both. Upload mode analyzes all discovered functions.")
        uploaded_direct = st.file_uploader(
            "Upload code files",
            accept_multiple_files=True,
            key="upload_direct_files",
        )
        uploaded_zip = st.file_uploader(
            "Upload ZIP (optional)",
            type=["zip"],
            key="upload_zip_file",
        )
        if st.button("Prepare uploads", key="prepare_uploads"):
            try:
                with st.status("Preparing uploaded files", expanded=False) as status:
                    direct_payload = [
                        (f.name, f.getvalue())
                        for f in (uploaded_direct or [])
                    ]
                    zip_blob = uploaded_zip.getvalue() if uploaded_zip else None
                    if not direct_payload and not zip_blob:
                        raise RuntimeError("Upload at least one direct file or one ZIP archive.")
                    src, files = materialize_uploaded_sources(
                        direct_payload,
                        zip_blob,
                        _DEFAULT_CODE_EXTS,
                    )
                    ss.upload_src_path = src
                    ss.upload_files = files
                    status.update(label=f"Prepared {len(files)} file(s)", state="complete")
            except Exception as exc:
                st.error(str(exc))
        if ss.upload_files:
            st.caption(f"Prepared {len(ss.upload_files)} file(s) for upload mode.")

can_run = (
    (mode == "Pull Request" and pr is not None)
    or (mode == "Commit" and commit is not None)
    or (mode == "Manual Files" and bool(ss.manual_files) and bool(ss.manual_ref))
    or (mode == "Upload Files" and bool(ss.upload_files) and bool(ss.upload_src_path))
)

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
                status.write("Building per-function prompts and chains...")
                prompts = build_prompts_by_function(cg, src, raw, depth=depth)
                chains = build_changed_file_chains(cg, raw, depth=depth)
            elif mode == "Commit":
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
            elif mode == "Manual Files":
                target_ref = ss.manual_ref
                target_repo = repo
                status.write("Preparing manual file selection source...")
                if not ss.src_path or ss.manual_loaded_ref != target_ref:
                    src = ss.gh.download_source(target_repo, target_ref)
                    ss.src_path = src
                    ss.manual_loaded_ref = target_ref
                    ss.manual_repo_files = _list_repo_files(src)
                else:
                    src = ss.src_path

                selected = [f for f in ss.manual_files if f in set(ss.manual_repo_files)]
                if not selected:
                    raise RuntimeError("Manual mode requires at least one selected file.")

                status.write("Building graph...")
                cg = build_graph(src, backend=ss.backend)
                status.write("Building per-function prompts and chains...")
                prompts = build_prompts_for_selected_files(cg, src, selected, depth=depth)
                chains = build_selected_file_chains(cg, selected, depth=depth)
                raw = _manual_diff_overview(selected)
                if not prompts:
                    status.write("No prompts were generated from selected files.")
            else:
                target_ref = "uploaded-files"
                target_repo = repo
                status.write("Preparing uploaded source...")
                src = ss.upload_src_path
                selected = [f for f in ss.upload_files if f in set(_list_repo_files(src))]
                if not selected:
                    raise RuntimeError("Upload mode requires at least one supported selected file.")

                status.write("Building graph...")
                cg = build_graph(src, backend=ss.backend)
                status.write("Building per-function prompts and chains...")
                prompts = build_prompts_for_selected_files(cg, src, selected, depth=depth)
                chains = build_selected_file_chains(cg, selected, depth=depth)
                raw = _manual_diff_overview(selected)
                if not prompts:
                    status.write("No prompts were generated from uploaded files.")

            ss.src_path = src
            ss.diff_raw = raw
            ss.prompts = prompts
            ss.file_chains = chains
            ss.target_ref = target_ref
            ss.review_mode = mode
            if mode == "Manual Files":
                prev_selected = dict(ss.manual_prompt_selected)
                ss.manual_prompt_selected = {
                    pid: bool(prev_selected.get(pid, True)) for pid in prompts.keys()
                }
            if not prompts:
                status.write("No prompts were generated from changed lines. Check diff overview and graph backend.")
            status.update(label="Prompt build complete", state="complete")
    except Exception as exc:
        st.error(str(exc))

manual_selected_prompt_ids: List[str] = []
if ss.review_mode == "Manual Files" and ss.prompts:
    st.subheader("Manual function selection")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Select all functions", key="manual_select_all_functions"):
            for pid in ss.prompts.keys():
                ss.manual_prompt_selected[pid] = True
    with col2:
        if st.button("Clear function selection", key="manual_clear_functions"):
            for pid in ss.prompts.keys():
                ss.manual_prompt_selected[pid] = False

    for prompt_id in sorted(ss.prompts.keys()):
        default_checked = bool(ss.manual_prompt_selected.get(prompt_id, True))
        checked = st.checkbox(
            prompt_id,
            key=f"manual_prompt_select::{prompt_id}",
            value=default_checked,
        )
        ss.manual_prompt_selected[prompt_id] = bool(checked)
        if checked:
            manual_selected_prompt_ids.append(prompt_id)

    st.caption(f"Selected {len(manual_selected_prompt_ids)} of {len(ss.prompts)} functions for review.")
elif ss.prompts:
    manual_selected_prompt_ids = list(ss.prompts.keys())

can_review = bool(ss.src_path) and (
    (
        ss.review_mode == "Manual Files"
        and bool(ss.prompts)
        and bool(ss.manual_files)
        and bool(manual_selected_prompt_ids)
    )
    or (
        ss.review_mode == "Upload Files"
        and bool(ss.prompts)
        and bool(ss.upload_files)
    )
    or (ss.review_mode != "Manual Files" and bool(ss.diff_raw))
)
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
            if ss.review_mode == "Manual Files":
                selected_prompt_ids = [
                    pid for pid in manual_selected_prompt_ids if pid in ss.prompts
                ]
                prompt_subset = {pid: ss.prompts[pid] for pid in selected_prompt_ids}
                chain_subset = {
                    pid: ss.file_chains[pid]
                    for pid in selected_prompt_ids
                    if pid in ss.file_chains
                }
                reviews = run_two_agent_review_manual(
                    cg,
                    ss.src_path,
                    ss.manual_files,
                    prompt_subset,
                    chain_subset,
                    complete,
                    max_workers=int(nova_workers),
                )
            elif ss.review_mode == "Upload Files":
                selected_prompt_ids = sorted(ss.prompts.keys())
                prompt_subset = {pid: ss.prompts[pid] for pid in selected_prompt_ids}
                chain_subset = {
                    pid: ss.file_chains[pid]
                    for pid in selected_prompt_ids
                    if pid in ss.file_chains
                }
                reviews = run_two_agent_review_manual(
                    cg,
                    ss.src_path,
                    ss.upload_files,
                    prompt_subset,
                    chain_subset,
                    complete,
                    max_workers=int(nova_workers),
                )
            else:
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
