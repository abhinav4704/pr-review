# Full-Codebase Analyser — Plan & Status

> Status doc. Captures **what we're supposed to build** and **what is done so far**.
> Nothing in `primitive-pr/` is touched — this analyser lives in its own folder and
> reuses `primitive-pr/pr_review` read-only.

---

## 1. Goal

A whole-repo analyser (no diff required).

- **Primary:** issue audit — *"what breaks what"* across the codebase as it currently is.
- **Bonus (if free):** comprehension — god nodes, modules/communities.
- **Hard constraints:**
  - Built in a **separate folder**; `primitive-pr/` is **never edited**.
  - LLM runs **at most once per file**, token-managed (reuse `pass_whole_file` / Agent 1).
  - Deterministic graph + scanners do the heavy lifting for free; LLM is a bounded top-up.

---

## 2. Folder layout (target)

```
pr-review/pr-review/
  primitive-pr/                 # UNCHANGED — existing PR tool + pr_review package
  codebase-analyser/            # THIS PROJECT
    PLAN.md                     # <- this file
    analyser/
      __init__.py
      paths.py                  # sys.path insert -> import pr_review read-only
      audit.py                  # orchestrator + AuditResult + report
      ranking.py                # deterministic file risk score (for LLM top-K)
      checks/                   # deterministic, from the in-memory CodeGraph
        breakage.py  deadcode.py  architecture.py
      extract_unresolved.py     # (Phase 1.5) capture broken refs before resolution
      scanners/                 # text/manifest scanners (no LLM)
        secrets.py  dependencies.py
      comprehension.py          # god nodes + communities (networkx, no vendor deps)
    audit_frontend.py           # Streamlit page
    requirements.txt
```

---

## 3. Pipeline

```
source (download_source or local path)
  -> pr_review.graph.build_graph(root, backend="primitive")     # in-memory CodeGraph
  -> (optional) neo4j_store.push(cg, pr_ref="repo@<sha>")        # persist/explore
  -> deterministic audit (free, whole repo)
  -> rank files by risk
  -> LLM: pass_whole_file on top-K files (1 call/file, budgeted; budget 0 = free run)
  -> AuditResult (sections + health score) -> markdown report + Streamlit tabs
```

---

## 4. What we reuse (read-only) from `primitive-pr/pr_review`

- `graph.build_graph`, `CodeGraph` methods (`fan_in`, `callers`, `reverse_dependents`,
  `routes`/`events`, `defs_in_file`).
- `neo4j_store` (optional persist + queries: `kind_counts`, `module_edges`, `top_fan_in`,
  `god_classes`, `top_subclassed`, `entrypoints`, `orphans`).
- `architecture.build_digest` / `digest_to_markdown` / `detect_module_cycles`.
- `impact.reverse_dependents` + helpers (blast radius / "what breaks what").
- `pr_passes.pass_whole_file` + `WHOLE_FILE_SYSTEM`; `review_llm.make_completion_fn`.
- `findings.Finding`, `severity_counts`, `SEV_BADGE`, `sort_by_severity`, `dedupe`.
- Comprehension: god-node detection and module community detection computed directly
  from the in-memory `CodeGraph` using `networkx` — `cg.fan_in()` for god nodes,
  `networkx.algorithms.community.greedy_modularity_communities` for module communities.
  No vendored modules required (`vendor_graph/` has been deleted).

---

## 5. Audit sections

- **Breakage — "what breaks what"** (core). See key finding in §7: unresolved refs are
  dropped by the graph build, so:
  - **v1 (robust): blast-radius map.** For riskiest nodes (high fan-in, sensitive names,
    entrypoints) use `reverse_dependents` -> "changing/breaking X takes down A, B, C".
  - **v2 (Phase 1.5): missing-symbol detection** via `extract_unresolved.py` (capture
    `__unresolved__::*` before resolution; flag names with no repo def and not stdlib/ext).
- **Dead code** — orphans (fan-in 0, non-test) -> `suggestion`.
- **Architecture smells** — cycles, god classes, hotspots, module coupling, entrypoints.
- **Secrets** (scanner) — provider regexes + entropy.
- **Dependencies** (scanner) — manifests + OSV.dev CVEs + unused/undeclared.
- **LLM review** — one `pass_whole_file` per top-K file.
- **Comprehension** — god nodes (high fan-in via `cg.fan_in()`) + module communities
  (networkx greedy modularity on module-level graph projection; no vendor dependency).

Decision: deterministic checks compute from the **in-memory CodeGraph** so the audit runs
without requiring a live Neo4j; Neo4j push stays optional (persistence/exploration). This
still honours the "no lossy traversal" concern (full-graph math, not DISTINCT neighbour
queries).

---

## 6. Phasing (independently shippable)

1. **Phase 1** — scaffold + `checks/` (breakage blast-radius, dead code, architecture) +
   `audit.py` + Streamlit Overview/Breakage/Architecture/Dead-code tabs. No new deps, no LLM.
2. **Phase 1.5** — `extract_unresolved.py` missing-symbol breakage.
3. **Phase 2** — scanners: secrets, then dependencies (+OSV).
4. **Phase 3** — LLM per-file pass (risk-ranked, budgeted) + LLM tab.
5. **Phase 4** — comprehension tab (god nodes/communities) + markdown report download.

---

## 7. Key findings from exploration

- `graph._resolve` (graph.py:750-752) **deletes all `__unresolved__` placeholder nodes**
  after resolution. The built graph keeps NO record of broken references -> v1 breakage must
  use the blast-radius map; genuine missing-symbol detection needs a pre-resolution capture
  (Phase 1.5).
- `architecture.build_digest` requires a live Neo4j `store`. To avoid forcing a DB on every
  run, Phase 1 computes the same facts from the in-memory `CodeGraph` (networkx is trivial
  for fan-in / cycles / method counts).
- `pass_whole_file` already does exactly one logical LLM call per file (chunked only when
  oversized) -> satisfies the "<=1 call per file" constraint with no new LLM code.

---

## 8. Status

### Done
- Architecture designed and approved (this plan).
- Reuse surface mapped in `primitive-pr/pr_review` (graph, neo4j_store, architecture,
  impact, pass_whole_file, findings).
- Key constraint discovered: unresolved refs are dropped at build (affects breakage design).
- `vendor_graph/` folder and `graphify_adapter.py` **deleted** from `primitive-pr/pr_review/`;
  comprehension phase now uses networkx directly — no vendor dependency.
- **No code written yet** for the analyser.

### To do (nothing started)
- [ ] Phase 1: scaffold folder + `paths.py` + `__init__.py` + `requirements.txt`
- [ ] Phase 1: `checks/breakage.py` (blast-radius), `checks/deadcode.py`, `checks/architecture.py`
- [ ] Phase 1: `audit.py` (run_audit, AuditResult, format_audit_report) — LLM budget default 0
- [ ] Phase 1: `audit_frontend.py` (Overview / Breakage / Architecture / Dead-code tabs)
- [ ] Phase 1: verify on a fixture repo (orphan, cycle, high-fan-in hub)
- [ ] Phase 1.5: `extract_unresolved.py` missing-symbol breakage
- [ ] Phase 2: `scanners/secrets.py`, `scanners/dependencies.py` (+OSV)
- [ ] Phase 3: `ranking.py` + LLM per-file pass (budgeted) + LLM tab
- [ ] Phase 4: `comprehension.py` (god nodes/communities) + markdown report

### Not changing
- `primitive-pr/` and its `pr_review` package — reused read-only via `analyser/paths.py`.
- `vendor_graph/` is permanently removed; do not add it back.
