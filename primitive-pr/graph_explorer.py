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

import json
import os
import re
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
ss.setdefault("synthesis_results", None)
ss.setdefault("per_file_change_impact", None)
ss.setdefault("impact_depth", 3)
ss.setdefault("impact_verify", True)
ss.setdefault("arch_digest_md", None)
ss.setdefault("arch_review_md", None)
ss.setdefault("provider_key", "nova")
ss.setdefault("llm_creds", {})
ss.setdefault("budget", 12000)
ss.setdefault("review_mode", "Pull Request")
ss.setdefault("commits", [])

# Invalidate cached GitHubClient if it is missing new methods (stale from hot-reload).
_CLIENT_METHODS = ("list_commits", "get_commit_diff", "list_branches")
if ss.gh is not None and not all(hasattr(ss.gh, m) for m in _CLIENT_METHODS):
    ss.gh = None
    ss.user = None
    ss.repos = []

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


def snippet_around_line(src_path: str, file_path: str, line: int, radius: int = 2) -> str:
    """Return a short numbered snippet around file:line for evidence display."""
    if not src_path or not file_path or line <= 0:
        return ""
    full = Path(src_path) / file_path
    if not full.exists():
        return ""
    lines = full.read_text(errors="replace").splitlines()
    start = max(1, line - radius)
    end = min(len(lines), line + radius)
    out = []
    for lineno in range(start, end + 1):
        mark = ">" if lineno == line else " "
        out.append(f"{lineno:>5} {mark}| {lines[lineno - 1]}")
    return "\n".join(out)


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


CONFIDENCE_CHIP = {"high": "🟢 High", "medium": "🟡 Medium", "low": "🔴 Low"}


def render_synthesis() -> None:
    """Primary output panel — synthesized issue reports (Track C)."""
    from pr_review.findings import SEV_BADGE, severity_counts
    reports = ss.synthesis_results
    if not reports:
        st.info(
            "No synthesized issues. Either no findings were produced by Track A/B, "
            "or synthesis found nothing actionable across the clusters."
        )
        return

    counts: dict = {}
    for r in reports:
        counts[r.severity] = counts.get(r.severity, 0) + 1
    st.markdown(
        "  ·  ".join(
            f"{SEV_BADGE[s]} **{s}**: {counts[s]}"
            for s in ("critical", "high", "medium", "low")
            if s in counts
        )
        + f"  ·  **{len(reports)} issue(s) synthesized**"
    )

    for i, r in enumerate(reports):
        badge = SEV_BADGE.get(r.severity, "")
        conf = CONFIDENCE_CHIP.get(r.confidence, r.confidence)
        suffix = " (unassigned bucket)" if getattr(r, "is_unassigned_bucket", False) else ""
        header = f"{badge} **[{r.severity.upper()}] {r.title}** — confidence {conf}{suffix}"
        with st.expander(header, expanded=(r.severity in ("critical", "high"))):
            # Root cause
            st.markdown("**Root cause**")
            if r.root_cause_file:
                loc = f"`{r.root_cause_file}`"
                if r.root_cause_line:
                    loc += f" line {r.root_cause_line}"
                st.markdown(f"_{loc}_")
                snippet = snippet_around_line(ss.src_path, r.root_cause_file, r.root_cause_line)
                if snippet:
                    st.code(snippet)
            if r.root_cause_summary:
                st.write(r.root_cause_summary)

            # Exploit / attack path
            if r.exploit_path:
                st.markdown("**Exploit / attack path**")
                # If the LLM used numbered lines, render as-is; else wrap in blockquote
                if re.search(r"^\s*\d+\.", r.exploit_path, re.MULTILINE):
                    st.markdown(r.exploit_path)
                else:
                    for step in r.exploit_path.splitlines():
                        if step.strip():
                            st.markdown(f"- {step.strip()}")

            # Blast radius
            if r.impact_chain:
                st.markdown("**Blast radius**")
                st.markdown(f"`{r.impact_chain}`")
            if r.affected_files:
                st.markdown(
                    "Affected files: "
                    + "  ·  ".join(f"`{f}`" for f in r.affected_files)
                )

            # Recommendation
            if r.recommendation:
                st.markdown("**Recommendation**")
                st.write(r.recommendation)

            # Evidence by file (small snippets / extracted evidence)
            ev = getattr(r, "evidence_by_file", None) or {}
            if ev:
                st.markdown("**Evidence By File**")
                for path, items in ev.items():
                    with st.expander(f"`{path}` evidence ({len(items)})", expanded=False):
                        for item in items:
                            st.code(item)

            # Provenance/no-loss accounting
            src_ids = getattr(r, "source_finding_ids", []) or []
            if src_ids:
                st.caption(f"Provenance: {len(src_ids)} source finding(s) attached")
            unassigned = getattr(r, "unassigned_ids", []) or []
            if unassigned:
                st.warning(f"Unassigned findings in this bucket: {len(unassigned)}")

            # Source findings (Track A + B evidence)
            if r.source_findings:
                with st.expander(
                    f"Source findings merged into this issue ({len(r.source_findings)})",
                    expanded=False,
                ):
                    for f in r.source_findings:
                        st.markdown(
                            f"{SEV_BADGE.get(f.severity, '')} **[{f.severity.upper()}] "
                            f"{f.title}** — `{f.file}:{f.line}`"
                        )
                        if f.explanation:
                            st.caption(f.explanation)
                        if f.file and f.line:
                            snippet = snippet_around_line(ss.src_path, f.file, f.line)
                            if snippet:
                                st.code(snippet)
                        st.divider()


def render_per_file_changes() -> None:
    """Separate section for raw per-file change view + mapped changed nodes."""
    if not ss.diff_files:
        st.info("No diff loaded.")
        return

    node_names_by_file = {}
    if ss.cg is not None and ss.diff_raw:
        from pr_review.diff import map_changes, parse_diff
        for cn in map_changes(ss.cg, parse_diff(ss.diff_raw)):
            d = ss.cg.node(cn.node_id) if ss.cg.has(cn.node_id) else {}
            path = d.get("path", "")
            label = d.get("qualname") or d.get("name") or cn.node_id
            if path:
                node_names_by_file.setdefault(path, []).append(
                    f"{label} ({cn.change_type})"
                )

    for f in ss.diff_files:
        added = sum(1 for l in f["lines"] if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in f["lines"] if l.startswith("-") and not l.startswith("---"))
        nodes = sorted(set(node_names_by_file.get(f["path"], [])))
        with st.expander(f"`{f['path']}`  +{added} / -{removed}", expanded=False):
            if nodes:
                st.markdown("**Changed nodes mapped from graph**")
                for n in nodes:
                    st.markdown(f"- {n}")
            else:
                st.caption("No changed nodes mapped in graph for this file.")

            st.markdown("**Changed lines preview**")
            changed = [
                l for l in f["lines"]
                if (l.startswith("+") and not l.startswith("+++"))
                or (l.startswith("-") and not l.startswith("---"))
            ]
            preview = changed[:40]
            st.code("\n".join(preview) if preview else "(no +/- body lines)", language="diff")
            if len(changed) > len(preview):
                st.caption(f"+{len(changed) - len(preview)} more changed line(s) hidden")


def render_per_file_change_impact_outputs() -> None:
    """Additive output block: what changed per file and downstream impact."""
    from pr_review.file_impact_outputs import render_per_file_change_impact_markdown

    items = ss.per_file_change_impact or []
    if not items:
        st.info("No per-file change-impact outputs available yet.")
        return

    md = render_per_file_change_impact_markdown(items)
    js = json.dumps(items, indent=2)

    c1, c2 = st.columns(2)
    c1.download_button(
        "Download per-file impact (markdown)",
        md,
        file_name="per_file_change_impact.md",
    )
    c2.download_button(
        "Download per-file impact (json)",
        js,
        file_name="per_file_change_impact.json",
    )

    for it in items:
        impact = it["impact"]
        header = (
            f"`{it['file']}`  +{it['added_lines']} / -{it['removed_lines']}"
            f"  · clusters {impact['cluster_count']}"
            f"  · downstream files {len(impact['downstream_files'])}"
            f"  · impact findings {len(impact['impact_findings'])}"
            f"  · risk {impact['highest_cluster_risk']}"
        )
        with st.expander(header, expanded=False):
            st.markdown("**Changed nodes**")
            if it["changed_nodes"]:
                for n in it["changed_nodes"]:
                    st.markdown(
                        f"- `{n['kind']}` `{n['name']}` "
                        f"(lines {n['start_line']}-{n['end_line']}, {n['change_type']})"
                    )
            else:
                st.caption("No graph-mapped changed nodes for this file.")

            st.markdown("**Impact summary**")
            st.markdown(f"- Local findings (Track A): {it['local_findings']}")
            st.markdown(
                f"- Downstream files: {len(impact['downstream_files'])}"
            )
            st.markdown(
                f"- Downstream consumers: {len(impact['downstream_consumers'])}"
            )

            if it.get("per_file_review_findings"):
                st.markdown("**Per-file review findings (Track A)**")
                for f in it["per_file_review_findings"]:
                    st.markdown(
                        f"- **[{f['severity'].upper()}] {f['title']}** "
                        f"(`{f['file']}:{f['line']}` · {f['category']})"
                    )

            if impact["downstream_files"]:
                st.markdown("Downstream file list:")
                for p in impact["downstream_files"]:
                    st.markdown(f"- `{p}`")

            if impact["impact_findings"]:
                st.markdown("**Impact findings (Track B)**")
                for f in impact["impact_findings"]:
                    st.markdown(
                        f"- **[{f['severity'].upper()}] {f['title']}** "
                        f"(`{f['file']}:{f['line']}` · {f['category']})"
                    )
            else:
                st.caption("No impact findings mapped to this file's change clusters.")


def render_raw_appendices() -> None:
    """Raw appendices so no information is hidden behind synthesis."""
    from pr_review.synthesis import finding_id

    with st.expander("Appendix: Per-Cluster Findings", expanded=False):
        reviews = ss.impact_reviews or []
        for idx, rev in enumerate(reviews, 1):
            members = ", ".join(
                ss.cg.node(m).get("qualname", m)
                for m in rev.cluster.members if ss.cg and ss.cg.has(m)
            )
            st.markdown(f"**Cluster {idx}:** {members}")
            if not rev.findings:
                st.caption("No impact findings")
            for f in rev.findings:
                st.markdown(
                    f"- `{finding_id(f)}` · **[{f.severity.upper()}] {f.title}** "
                    f"(`{f.file}:{f.line}`)"
                )

    with st.expander("Appendix: Per-Cluster Impact Chains", expanded=False):
        reviews = ss.impact_reviews or []
        for idx, rev in enumerate(reviews, 1):
            st.markdown(f"**Cluster {idx}**")
            if not rev.cluster.chains:
                st.caption("No chains")
            for ch in rev.cluster.chains:
                st.markdown(f"- {_chain_md(ch)}")

    with st.expander("Appendix: Raw Findings (Track A + Track B)", expanded=False):
        from pr_review.findings import sort_by_severity

        a_findings = [f for fs in (ss.pr_review_results or {}).values() for f in fs]
        b_findings = [f for rev in (ss.impact_reviews or []) for f in rev.findings]
        all_findings = sort_by_severity(a_findings + b_findings)
        st.caption(f"Total raw findings: {len(all_findings)}")
        for f in all_findings:
            st.markdown(
                f"- `{finding_id(f)}` · **[{f.severity.upper()}] {f.title}** "
                f"(`{f.file}:{f.line}` · {f.category})"
            )


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
# Combined per-file output (primary view)
# ─────────────────────────────────────────────────────────────────────────────

def _build_combined_report_md() -> str:
    """Generate a combined markdown report for all changed files (all three tracks)."""
    from pr_review.findings import SEV_BADGE, sort_by_severity

    lines = ["# PR Review — Per-File Combined Report\n"]
    for f in (ss.diff_files or []):
        file_path = f["path"]
        added = sum(1 for ln in f["lines"] if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in f["lines"] if ln.startswith("-") and not ln.startswith("---"))
        lines.append(f"\n---\n\n## `{file_path}`  (+{added} / -{removed})\n")

        # Track C
        synth = [r for r in (ss.synthesis_results or []) if file_path in (r.affected_files or [])]
        lines.append("\n### Track C — Synthesized Issues\n")
        if synth:
            for r in synth:
                badge = SEV_BADGE.get(r.severity, "")
                lines.append(f"\n#### {badge} [{r.severity.upper()}] {r.title}\n")
                lines.append(f"**Confidence:** {r.confidence}  ")
                if r.root_cause_file:
                    loc = f"`{r.root_cause_file}`" + (f" line {r.root_cause_line}" if r.root_cause_line else "")
                    lines.append(f"**Root cause:** {loc}  ")
                if r.root_cause_summary:
                    lines.append(f"\n{r.root_cause_summary}\n")
                if r.exploit_path:
                    lines.append(f"\n**Exploit / attack path:**\n{r.exploit_path}\n")
                if r.impact_chain:
                    lines.append(f"\n**Blast radius:** `{r.impact_chain}`\n")
                if r.affected_files:
                    lines.append("**Affected files:** " + ", ".join(f"`{af}`" for af in r.affected_files))
                if r.recommendation:
                    lines.append(f"\n**Recommendation:** {r.recommendation}\n")
        else:
            lines.append("_No synthesized issues for this file._\n")

        # Track A
        a_findings = (ss.pr_review_results or {}).get(file_path, [])
        lines.append("\n### Track A — Per-File Review Findings\n")
        if a_findings:
            for fi in sort_by_severity(a_findings):
                badge = SEV_BADGE.get(fi.severity, "")
                lines.append(f"\n- {badge} **[{fi.severity.upper()}] {fi.title}** (line {fi.line} · {fi.category})")
                if fi.explanation:
                    lines.append(f"  \n  {fi.explanation}")
                if fi.recommendation:
                    lines.append(f"  \n  **Fix:** {fi.recommendation}")
            lines.append("")
        else:
            lines.append("_No per-file findings._\n")

        # Track B
        impact_by_file = {it["file"]: it for it in (ss.per_file_change_impact or [])}
        impact_item = impact_by_file.get(file_path)
        lines.append("\n### Track B — Per-File Change Impact\n")
        if impact_item:
            impact = impact_item["impact"]
            lines.append(f"- Local findings (Track A): {impact_item['local_findings']}")
            lines.append(f"- Clusters: {impact['cluster_count']}")
            lines.append(f"- Downstream files: {len(impact['downstream_files'])}")
            lines.append(f"- Downstream consumers: {len(impact['downstream_consumers'])}")
            lines.append(f"- Highest cluster risk: {impact['highest_cluster_risk']}\n")
            if impact_item["changed_nodes"]:
                lines.append("**Changed nodes:**")
                for n in impact_item["changed_nodes"]:
                    lines.append(f"- `{n['kind']}` `{n['name']}` (lines {n['start_line']}-{n['end_line']}, {n['change_type']})")
                lines.append("")
            if impact["downstream_files"]:
                lines.append("**Downstream files:**")
                for p in impact["downstream_files"]:
                    lines.append(f"- `{p}`")
                lines.append("")
            if impact["impact_findings"]:
                lines.append("**Impact findings (Track B):**")
                for fi in impact["impact_findings"]:
                    lines.append(f"- **[{fi['severity'].upper()}] {fi['title']}** (`{fi['file']}:{fi['line']}` · {fi['category']})")
                lines.append("")
        else:
            lines.append("_No impact data for this file._\n")

    return "\n".join(lines)


def render_final_per_file_output() -> None:
    """Unified per-file output: Track C (synthesized) + Track A (per-file) + Track B (impact)."""
    from pr_review.findings import SEV_BADGE, sort_by_severity

    if not ss.diff_files:
        st.info("No diff loaded.")
        return

    impact_by_file = {it["file"]: it for it in (ss.per_file_change_impact or [])}

    md = _build_combined_report_md()
    st.download_button(
        "⬇ Download combined report (markdown)",
        md,
        file_name="pr_review_combined.md",
        mime="text/markdown",
    )

    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}

    for f in ss.diff_files:
        file_path = f["path"]
        added = sum(1 for ln in f["lines"] if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in f["lines"] if ln.startswith("-") and not ln.startswith("---"))

        synth_for_file = [r for r in (ss.synthesis_results or []) if file_path in (r.affected_files or [])]
        a_findings = (ss.pr_review_results or {}).get(file_path, [])
        impact_item = impact_by_file.get(file_path)
        b_findings = (impact_item or {}).get("impact", {}).get("impact_findings", []) if impact_item else []

        all_sevs = (
            [r.severity for r in synth_for_file]
            + [fi.severity for fi in a_findings]
            + [fi["severity"] for fi in b_findings]
        )
        sev_summary = ""
        if all_sevs:
            worst = min(all_sevs, key=lambda s: sev_order.get(s, 99))
            sev_summary = f"  · {SEV_BADGE.get(worst, '')} {worst}"

        header = f"`{file_path}`  +{added} / -{removed}{sev_summary}"
        expanded = any(s in ("critical", "high") for s in all_sevs)

        with st.expander(header, expanded=expanded):

            # ── Track C ───────────────────────────────────────────────────
            st.markdown("#### 🔬 Track C — Synthesized Issues")
            if synth_for_file:
                for r in synth_for_file:
                    badge = SEV_BADGE.get(r.severity, "")
                    conf = CONFIDENCE_CHIP.get(r.confidence, r.confidence)
                    suffix = " (unassigned bucket)" if getattr(r, "is_unassigned_bucket", False) else ""
                    st.markdown(f"**{badge} [{r.severity.upper()}] {r.title}** — confidence {conf}{suffix}")
                    if r.root_cause_file:
                        loc = f"`{r.root_cause_file}`"
                        if r.root_cause_line:
                            loc += f" line {r.root_cause_line}"
                        st.markdown(f"_Root cause: {loc}_")
                        snip = snippet_around_line(ss.src_path, r.root_cause_file, r.root_cause_line)
                        if snip:
                            st.code(snip)
                    if r.root_cause_summary:
                        st.write(r.root_cause_summary)
                    if r.exploit_path:
                        st.markdown("**Exploit / attack path**")
                        if re.search(r"^\s*\d+\.", r.exploit_path, re.MULTILINE):
                            st.markdown(r.exploit_path)
                        else:
                            for step in r.exploit_path.splitlines():
                                if step.strip():
                                    st.markdown(f"- {step.strip()}")
                    if r.impact_chain:
                        st.markdown(f"**Blast radius:** `{r.impact_chain}`")
                    if r.affected_files:
                        st.markdown("Affected files: " + "  ·  ".join(f"`{af}`" for af in r.affected_files))
                    if r.recommendation:
                        st.markdown(f"**Recommendation:** {r.recommendation}")
                    ev = getattr(r, "evidence_by_file", None) or {}
                    if ev:
                        with st.expander(f"Evidence by file ({len(ev)})", expanded=False):
                            for path, evitems in ev.items():
                                st.markdown(f"`{path}`")
                                for item in evitems:
                                    st.code(item)
                    st.divider()
            else:
                st.caption("No synthesized issues for this file.")

            # ── Track A ───────────────────────────────────────────────────
            st.markdown("#### 📋 Track A — Per-File Review Findings")
            if a_findings:
                for fi in sort_by_severity(a_findings):
                    st.markdown(
                        f"**{SEV_BADGE.get(fi.severity, '')} [{fi.severity.upper()}] "
                        f"{fi.title}**  \n_line {fi.line} · {fi.category}_"
                    )
                    if fi.explanation:
                        st.write(fi.explanation)
                    if fi.evidence:
                        st.code(fi.evidence)
                    if fi.recommendation:
                        st.markdown(f"**Fix:** {fi.recommendation}")
                    st.divider()
            else:
                st.caption("No per-file findings.")

            # ── Track B ───────────────────────────────────────────────────
            st.markdown("#### 📦 Track B — Per-File Change Impact")
            if impact_item:
                impact = impact_item["impact"]
                st.markdown("**Changed nodes**")
                if impact_item["changed_nodes"]:
                    for n in impact_item["changed_nodes"]:
                        st.markdown(
                            f"- `{n['kind']}` `{n['name']}` "
                            f"(lines {n['start_line']}-{n['end_line']}, {n['change_type']})"
                        )
                else:
                    st.caption("No graph-mapped changed nodes for this file.")

                st.markdown("**Impact summary**")
                st.markdown(f"- Local findings (Track A): {impact_item['local_findings']}")
                st.markdown(f"- Downstream files: {len(impact['downstream_files'])}")
                st.markdown(f"- Downstream consumers: {len(impact['downstream_consumers'])}")

                if impact_item.get("per_file_review_findings"):
                    st.markdown("**Per-file review findings (Track A)**")
                    for fi in impact_item["per_file_review_findings"]:
                        st.markdown(
                            f"- **[{fi['severity'].upper()}] {fi['title']}** "
                            f"(`{fi['file']}:{fi['line']}` · {fi['category']})"
                        )

                if impact["downstream_files"]:
                    st.markdown("Downstream file list:")
                    for p in impact["downstream_files"]:
                        st.markdown(f"- `{p}`")

                if impact["impact_findings"]:
                    st.markdown("**Impact findings (Track B)**")
                    for fi in impact["impact_findings"]:
                        st.markdown(
                            f"- **[{fi['severity'].upper()}] {fi['title']}** "
                            f"(`{fi['file']}:{fi['line']}` · {fi['category']})"
                        )
                else:
                    st.caption("No impact findings mapped to this file's change clusters.")
            else:
                st.caption("No impact data for this file.")


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
st.title("🔎 PR / Commit Analyzer")
st.caption("Graph-grounded, multi-pass LLM review of a pull request or commit.")

if not ss.gh:
    st.info("Connect to GitHub in the sidebar to begin.")
    st.stop()

repo = st.selectbox("Repository", ss.repos)

mode = st.radio("Review mode", ["Pull Request", "Commit"], horizontal=True)

from pr_review.github_client import GitHubError

# ── Target selection ──────────────────────────────────────────────────────────
pr = None
commit = None

if mode == "Pull Request":
    pulls = []
    if repo:
        try:
            pulls = ss.gh.list_pulls(repo)
        except GitHubError as e:
            st.error(str(e))
    if not pulls:
        st.warning("No open pull requests found for this repo.")
    else:
        pr_labels = {f"#{p.number} — {p.title}": p for p in pulls}
        pr = pr_labels[st.selectbox("Pull request", list(pr_labels.keys()))]

else:  # Commit mode
    branches = []
    if repo:
        try:
            branches = ss.gh.list_branches(repo)
        except GitHubError as e:
            st.error(str(e))
    branch = st.selectbox("Branch", branches) if branches else None
    commits: list = []
    if repo and branch:
        try:
            commits = ss.gh.list_commits(repo, branch)
        except GitHubError as e:
            st.error(str(e))
    if not commits:
        st.warning("No commits found for this branch." if branch else "Select a branch.")
    else:
        commit = st.selectbox(
            "Commit",
            commits,
            format_func=lambda c: f"{c.sha[:7]}  {c.date}  {c.author}: {c.message}",
        )

can_analyze = (mode == "Pull Request" and pr is not None) or \
              (mode == "Commit" and commit is not None)

col_a, col_b = st.columns([1, 3])
analyze = col_a.button(
    "Analyze PR" if mode == "Pull Request" else "Analyze Commit",
    type="primary",
    use_container_width=True,
    disabled=not can_analyze,
)
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

if analyze and can_analyze:
    store = get_store()
    if store is None:
        st.error("Neo4j is not configured (set URI + password in the sidebar) or the "
                 "`neo4j` driver is not installed.")
    else:
        status_label = "Analyzing PR…" if mode == "Pull Request" else "Analyzing Commit…"
        with st.status(status_label, expanded=True) as status:
            try:
                # Resolve target ref and repo for graph building
                if mode == "Pull Request":
                    target_ref = pr.head_ref
                    target_repo = pr.head_repo
                else:
                    target_ref = commit.sha
                    target_repo = repo

                # 1. graph (cached per target_ref unless rebuild)
                if rebuild or ss.graph_ref != target_ref or not ss.src_path \
                        or ss.cg is None:
                    from pr_review.graph import build_graph
                    short_ref = target_ref if len(target_ref) <= 20 else target_ref[:12] + "…"
                    status.write(f"Downloading `{target_repo}` at `{short_ref}`…")
                    src = ss.gh.download_source(target_repo, target_ref)
                    status.write("Building code graph…")
                    cg = build_graph(src)
                    status.write("Pushing graph to Neo4j…")
                    store.push(cg, pr_ref=target_ref)
                    ss.graph_ref = target_ref
                    ss.src_path = src
                    ss.cg = cg
                    ss.graph_stats = (cg.g.number_of_nodes(), cg.g.number_of_edges())
                else:
                    status.write("Using cached graph.")

                # 2. diff
                status.write("Loading diff…")
                if mode == "Pull Request":
                    raw = ss.gh.get_pr_diff(repo, pr.number)
                    ss.diff_pr_number = pr.number
                else:
                    raw = ss.gh.get_commit_diff(repo, commit.sha)
                    ss.diff_pr_number = None
                ss.diff_raw = raw
                ss.diff_files = parse_diff_files(raw)

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

                from pr_review.diff import map_changes
                from pr_review.file_impact_outputs import build_per_file_change_impact

                changed_nodes = map_changes(ss.cg, file_diffs)
                ss.per_file_change_impact = build_per_file_change_impact(
                    diff_files=ss.diff_files or [],
                    code_graph=ss.cg,
                    impact_reviews=ss.impact_reviews or [],
                    per_file_findings=ss.pr_review_results or {},
                    changed_nodes=changed_nodes,
                )

                # Track C — per-cluster synthesis
                from pr_review.synthesis import synthesize_all

                def _cb_synth(i, total, _name):
                    if total and i < total:
                        status.write(f"Synthesizing cluster {i + 1}/{total}…")

                status.write("Synthesizing findings into issue reports…")
                ss.synthesis_results = synthesize_all(
                    ss.cg,
                    ss.impact_reviews or [],
                    ss.pr_review_results or {},
                    complete_fn,
                    budget=ss.budget,
                    progress_cb=_cb_synth,
                )
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

# ── Unified per-file output (primary) ────────────────────────────────────────
if ss.diff_files is not None:
    st.divider()
    st.subheader("📋 Review Results — Per Changed File")
    st.caption(
        "Each changed file shows: Track C synthesized issues (combined A+B), "
        "Track A per-file findings, and Track B impact analysis."
    )
    render_final_per_file_output()

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
