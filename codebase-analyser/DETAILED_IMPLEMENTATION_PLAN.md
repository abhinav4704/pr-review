# Codebase Analyser Detailed Implementation Plan

This plan reflects the approved scope:
- Manual-first workflow now (repo/branch selected directly in UI)
- No CI in current phase
- No mandatory CLI in current phase
- Reuse `primitive-pr/pr_review` in read-only mode

## Current Status Snapshot

Implemented in this sprint:
- Phase 1 scaffold and package structure under `analyser/`
- Deterministic checks:
  - breakage blast-radius (`analyser/checks/breakage.py`)
  - dead code candidates (`analyser/checks/deadcode.py`)
  - architecture signals (`analyser/checks/architecture.py`)
- Audit orchestrator and markdown report formatter (`analyser/audit.py`)
- Manual Streamlit UI (`audit_frontend.py`)
- Smoke test (`tests/test_audit_smoke.py`)

Validated:
- `pytest tests -q` passes in `codebase-analyser`

## Execution Plan (Remaining Work)

## Phase 1 hardening (current)

1. Improve breakage confidence quality
- Add exclusion controls for generated/vendor/test paths.
- Add relation-level weighting (`calls` > `imports`) in blast ranking.
- Add per-node evidence lines via `caller_evidence_lines`.

2. Improve dead-code precision
- Exclude known framework callback patterns.
- Separate "likely-dead" vs "review-needed" buckets.

3. Improve architecture section
- Add module cycle compaction and clearer cycle readability.
- Add top coupled module pairs with trend-friendly format.

4. UI hardening
- Add finding filters (severity/category/path prefix).
- Add paging controls for large result sets.

5. Tests
- Add deterministic fixtures for:
  - cycle detection,
  - hotspot fan-in,
  - dead-code false-positive guardrails.

Exit criteria:
- Stable deterministic output across repeated runs on same repo.
- UI remains responsive on medium repositories.

## Phase 1.5 missing-symbol detection

1. Add `analyser/extract_unresolved.py`
- Capture unresolved references before graph placeholder cleanup.
- Build unresolved index keyed by symbol and source location.

2. Add missing-symbol findings
- Identify unresolved references with no in-repo definition.
- Exclude standard library and third-party module symbols.
- Map unresolved reference to impacted callers.

3. Integrate into audit
- Merge missing-symbol findings into Breakage tab.
- Add confidence labels and evidence snippets.

4. Tests
- Removed symbol fixture.
- Renamed symbol fixture.
- Ambiguous symbol fixture.

Exit criteria:
- Breakage section includes explicit missing-symbol findings with evidence.

## Phase 2 scanners (security + dependencies)

1. Add `analyser/scanners/secrets.py`
- Regex detector (API keys/tokens/passwords).
- Entropy signal for suspicious literals.
- Ignore/allowlist support.

2. Add `analyser/scanners/dependencies.py`
- Parse Python/JS manifests.
- Detect imported-not-declared and declared-not-used.
- Optional OSV enrichment with timeout/retry/fallback.

3. Integrate scanners
- Add sections to `AuditResult` and report formatter.
- Render scanner outputs in Streamlit tabs.

4. Tests
- False-positive controls for secrets.
- Dependency parser fixtures.
- OSV offline fallback behavior.

Exit criteria:
- Scanners add findings without breaking base deterministic run.

## Phase 3 bounded LLM augmentation (optional)

1. Add `analyser/ranking.py`
- Deterministic file risk score from graph metrics and finding density.

2. LLM pass integration
- Reuse `pr_review.pr_passes.pass_whole_file`.
- Enforce one-pass-per-file and top-K budget cap.
- Keep LLM stage fully optional and disabled by default.

3. Merge and dedupe
- Normalize LLM findings into shared schema.
- Dedupe against deterministic findings.

4. Tests
- Budget policy enforcement.
- Deduplication correctness.
- LLM-disabled path regression.

Exit criteria:
- LLM stage can be enabled manually and never blocks deterministic audit.

## Phase 4 comprehension + exports

1. Add `analyser/comprehension.py`
- God nodes (high fan-in concentration).
- Module communities via networkx modularity clustering.

2. Enhance report outputs
- Keep markdown export.
- Add stable JSON schema versioning.

3. UI additions
- Comprehension tab with ranked summaries.

4. Tests
- Community extraction shape test.
- Export schema consistency tests.

Exit criteria:
- One manual run produces complete deterministic/scanner/LLM/comprehension report bundle.

## Manual Operation Guide (current)

1. Install dependencies:
```powershell
pip install -r requirements.txt
```

2. Run app:
```powershell
streamlit run audit_frontend.py
```

3. In UI:
- Set repository root.
- Set breakage depth and top risky nodes.
- Click Run Audit.
- Review tabs and download markdown/json reports.

## Scope Guardrails

- Do not edit `primitive-pr/`; treat as read-only dependency.
- Keep manual-first UX as primary interface.
- Keep CI/CLI deferred and tracked only in `FUTURE.md`.
