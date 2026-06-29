"""Manual-first Streamlit UI for codebase-analyser deterministic audit."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import streamlit as st

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from analyser import format_audit_report, run_audit

st.set_page_config(
    page_title="Codebase Analyser",
    page_icon="🧭",
    layout="wide",
)

st.title("Codebase Analyser")
st.caption("Manual-first deterministic audit using in-memory graph analysis.")

with st.sidebar:
    st.header("Run Settings")
    default_repo = str(APP_ROOT.parent)
    repo_root = st.text_input("Repository root", value=default_repo)
    depth = st.slider("Breakage depth", min_value=1, max_value=5, value=2)
    top_n = st.slider("Top risky nodes", min_value=5, max_value=50, value=20, step=5)
    include_osv = st.checkbox("Enable OSV vulnerability lookup", value=False)
    run = st.button("Run Audit", type="primary")

if "audit_result" not in st.session_state:
    st.session_state.audit_result = None
if "audit_error" not in st.session_state:
    st.session_state.audit_error = ""

if run:
    if not os.path.isdir(repo_root):
        st.session_state.audit_error = f"Repository root does not exist: {repo_root}"
        st.session_state.audit_result = None
    else:
        try:
            with st.spinner("Building graph and running deterministic checks..."):
                st.session_state.audit_result = run_audit(
                    repo_root=repo_root,
                    depth=depth,
                    top_n=top_n,
                    include_osv=include_osv,
                )
                st.session_state.audit_error = ""
        except Exception as exc:
            st.session_state.audit_error = str(exc)
            st.session_state.audit_result = None

if st.session_state.audit_error:
    st.error(st.session_state.audit_error)

result = st.session_state.audit_result
if result is not None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Files", result.summary.files)
    c2.metric("Definitions", result.summary.definitions)
    c3.metric("Findings", result.summary.findings_total)
    c4.metric("Health", result.summary.health_score)

    tab_overview, tab_breakage, tab_architecture, tab_deadcode, tab_security, tab_dependencies, tab_report = st.tabs(
        ["Overview", "Breakage", "Architecture", "Dead Code", "Security", "Dependencies", "Report"]
    )

    with tab_overview:
        st.subheader("Summary")
        st.json(
            {
                "repo_root": result.summary.repo_root,
                "generated_at": result.summary.generated_at,
                "files": result.summary.files,
                "definitions": result.summary.definitions,
                "findings_total": result.summary.findings_total,
                "health_score": result.summary.health_score,
            }
        )

    with tab_breakage:
        st.subheader("Blast Radius")
        blast = result.breakage.get("blast_radius", [])
        if not blast:
            st.info("No breakage blast-radius findings.")
        else:
            for item in blast[:30]:
                with st.expander(
                    f"{item['severity'].upper()} · {item['source_name']} · impacts {item['impacted_count']}"
                ):
                    st.write(item["summary"])
                    st.dataframe(item["impacted"], use_container_width=True, hide_index=True)

        st.subheader("Hotspots")
        st.dataframe(result.breakage.get("hotspots", []), use_container_width=True, hide_index=True)

        st.subheader("Missing Symbols")
        missing = result.breakage.get("missing_symbols", [])
        if not missing:
            st.info("No unresolved in-repo symbol imports detected.")
        else:
            st.dataframe(missing, use_container_width=True, hide_index=True)

    with tab_architecture:
        st.subheader("Architecture Signals")
        st.markdown(f"Import cycles: **{len(result.architecture.get('import_cycles', []))}**")
        st.markdown(f"Entrypoints: **{len(result.architecture.get('entrypoints', []))}**")

        st.markdown("### Top Hotspots")
        st.dataframe(result.architecture.get("hotspots", []), use_container_width=True, hide_index=True)

        cycles = result.architecture.get("import_cycles", [])
        if cycles:
            st.markdown("### Import Cycles")
            for cycle in cycles[:20]:
                st.write(" -> ".join(cycle))

        st.markdown("### Module Coupling")
        st.dataframe(result.architecture.get("module_coupling", []), use_container_width=True, hide_index=True)

    with tab_deadcode:
        st.subheader("Dead Code Candidates")
        orphans = result.deadcode.get("orphans", [])
        if not orphans:
            st.info("No orphan function/method candidates found.")
        else:
            st.dataframe(orphans, use_container_width=True, hide_index=True)

    with tab_security:
        st.subheader("Potential Secrets")
        findings = result.security.get("findings", [])
        if not findings:
            st.info("No potential secrets detected.")
        else:
            st.dataframe(findings, use_container_width=True, hide_index=True)

    with tab_dependencies:
        st.subheader("Dependency Hygiene")
        st.markdown(f"OSV status: **{result.dependencies.get('osv_status', 'disabled')}**")
        st.markdown(
            f"Declared: **{result.dependencies.get('declared_count', 0)}** | "
            f"Imported: **{result.dependencies.get('imported_count', 0)}**"
        )
        findings = result.dependencies.get("findings", [])
        if not findings:
            st.info("No dependency findings.")
        else:
            st.dataframe(findings, use_container_width=True, hide_index=True)

    with tab_report:
        report_md = format_audit_report(result)
        st.code(report_md, language="markdown")
        st.download_button(
            "Download Markdown Report",
            data=report_md,
            file_name="codebase_audit_report.md",
            mime="text/markdown",
        )
        st.download_button(
            "Download JSON Report",
            data=json.dumps(
                {
                    "summary": result.summary.__dict__,
                    "breakage": result.breakage,
                    "deadcode": result.deadcode,
                    "architecture": result.architecture,
                    "security": result.security,
                    "dependencies": result.dependencies,
                },
                indent=2,
            ),
            file_name="codebase_audit_report.json",
            mime="application/json",
        )
else:
    st.info("Set a repository root and click Run Audit.")
