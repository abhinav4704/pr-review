# Plan: Full Codebase Analysis Feature

A new review mode that analyzes the **entire repository** — not just a PR diff —
covering security, dependencies, CI/CD workflows, architecture health, and test
coverage. It reuses the existing code graph, agents, profiles, and verifier, and
upgrades the graph where the current build is too diff-centric.

---

## 1. Goal & scope

Today the pipeline is strictly diff-driven: `parse_diff → map_changes →
blast_radius → run_review` only ever looks at files touched by a PR. The new
mode answers a different question: **"How healthy is this repo right now?"**

| Area | What it checks |
|---|---|
| **Secrets** | Hardcoded credentials, API keys, private keys, tokens — anywhere in the repo, including non-source files |
| **Security hotspots** | Injection, authn/authz gaps, unsafe deserialization, SSRF, crypto misuse — LLM review targeted at the riskiest files |
| **Dependencies** | Every manifest parsed; known CVEs (OSV.dev), unused declared deps, undeclared used deps, unpinned versions |
| **Workflows / CI** | GitHub Actions, Dockerfiles, docker-compose: unpinned actions, `pull_request_target` foot-guns, secret leakage, over-broad permissions |
| **Architecture** | Circular imports, dead code, god modules, layering violations, duplicate logic (via embeddings) |
| **Test coverage map** | Which public functions/routes have zero covering tests (graph-derived, no instrumentation) |

Design principle: **deterministic scanners first, LLM second.** Scanners are
free and run over the whole repo; LLM agents are expensive, so they only see a
risk-ranked top-K slice. This mirrors the existing Quick/Standard/Deep split.

---

## 2. Graph improvements (prerequisites)

The graph (`pr_review/graph.py`) was built for diff review and has gaps that
matter at repo scale. These changes benefit the existing PR mode too.

### 2.1 Per-file node index (performance — blocking)
`defs_in_file` (`graph.py:455`) scans every graph node; `node_for_line` calls it
per line. Repo-wide analysis calls these constantly. Add a `path -> [node_ids]`
dict to `CodeGraph`, populated in `_add_def` / `build_graph`. (Already finding
B2 in `CODE_REVIEW_FINDINGS.md`.)

### 2.2 External imports become nodes (needed by dependency analysis)
`_PyExtractor._handle_import` (`graph.py:290-310`) only adds `imports` edges
when the module resolves to a repo file (`module_index`) — external imports
(`requests`, `boto3`, `numpy`…) are silently dropped. The dependency auditor
must cross-reference *declared* deps vs *used* imports, so:

- When resolution fails, add a node `ext::<top_level_module>` with
  `kind="extdep"`, and an edge `file -[:imports]-> ext::<module>`.
- Same in `_ast_fallback` and (best-effort, from `import`/`require` statements)
  in `_GenericExtractor` for JS/TS.
- New accessor: `CodeGraph.external_imports() -> Dict[str, List[str]]`
  (module → importing files).

### 2.3 Module-level calls and decorators
`_visit` routes top-level `call` nodes to the `pass` stub `_handle_call`
(`graph.py:312`) — so module-level side effects (`app = Flask(__name__)`,
`engine = create_engine(DSN)`, `logging.basicConfig(...)`) produce **no edges**
and are invisible to dead-code and security analysis. Fix: attribute
module-level calls to the file node (`file -[:calls]-> target`), reusing
`_extract_calls_in_body` with `caller_id = self.path`.

### 2.4 Entrypoint detection (needed by dead-code analysis)
Dead-code = fan-in 0 is wrong for entrypoints. Mark nodes `is_entrypoint=True`
when:
- file contains `if __name__ == "__main__":` (tree-sitter: top-level `if` with
  that comparison),
- function referenced in `pyproject.toml [project.scripts]` / `setup.py
  entry_points` / `package.json "scripts"`/`"main"`,
- kind is `route`, `event`, or has a recognized framework decorator
  (`pytest.fixture`, `click.command`, `app.cli...`) — extend the existing
  `_ROUTE_DECORATORS`/`_EVENT_DECORATORS` sets with a `_ENTRYPOINT_DECORATORS`
  set.

### 2.5 Non-code files enter the graph as typed file nodes
`build_graph` only walks `EXT_TO_LANG` extensions. Add lightweight nodes
(no parsing, just `kind` + path + line count) for:
- `kind="workflow"`: `.github/workflows/*.yml|yaml`, `.gitlab-ci.yml`,
  `Jenkinsfile`, `azure-pipelines.yml`
- `kind="container"`: `Dockerfile*`, `docker-compose*.yml`
- `kind="manifest"`: everything in `filters.MANIFEST_FILENAMES` +
  `requirements*.txt` + lock files (reuse `filters._is_manifest` /
  `IGNORE_FILENAMES` for discovery — they stay excluded from *PR review* but the
  *audit* wants them)
- `kind="config"`: `*.toml`, `*.ini`, `*.cfg`, `*.env.example`

New accessors: `cg.workflows()`, `cg.manifests()` (same pattern as
`cg.routes()` / `cg.tables()` at `graph.py:485-489`).

### 2.6 Proper TypeScript grammar
`graph.py:46-47` reuses the JavaScript grammar for `.ts/.tsx`; type annotations
mis-parse and drop definitions. Try `tree_sitter_typescript` first, fall back
to JS. Matters more at repo scale (whole TS repos vs a few changed files).

---

## 3. New deterministic scanners (no LLM, run on everything)

New package: `pr_review/scanners/` — each scanner returns `List[Finding]`
(reusing `agents.Finding`), tagged with a new field `source: str = "llm"` →
scanners set `source="scanner"` so the UI can distinguish "pattern match" from
"model judgment". Findings from scanners **skip the LLM verifier** (they are
already deterministic).

### 3.1 `scanners/secrets.py`
Line-by-line regex scan of every text file (respecting `filters.IGNORE_DIRS` /
binary suffixes, but **including** configs, yaml, env files):
- Provider-specific patterns: AWS (`AKIA[0-9A-Z]{16}` + secret pair), GitHub
  (`ghp_`, `github_pat_`), Slack (`xox[bporas]-`), Google API (`AIza…`),
  Stripe (`sk_live_…`), private key blocks (`-----BEGIN … PRIVATE KEY-----`),
  JWTs, generic `password|secret|token\s*[:=]\s*["'][^"']{8,}`.
- Shannon-entropy check (>4.0 over 20+ chars) on assignment RHS to catch
  unknown formats; suppress matches in test fixtures and `*.example` files
  (configurable allowlist, plus inline `# pragma: allowlist-secret`).
- Severity: `critical` for provider-verified formats, `high` for entropy hits.

### 3.2 `scanners/dependencies.py`
1. **Parse manifests** found via the graph (`cg.manifests()`):
   `requirements*.txt`, `pyproject.toml` (`[project.dependencies]`, poetry),
   `package.json` (+lockfile versions), `go.mod`, `Cargo.toml` — into a common
   `Dependency(name, version_spec, resolved_version, ecosystem, manifest_path,
   line)` dataclass.
2. **Known vulnerabilities** via the free **OSV.dev batch API**
   (`POST https://api.osv.dev/v1/querybatch`, no key needed): one request per
   ~100 deps, mapped back to findings with CVE ids, severity from CVSS, and the
   fixed version as the recommendation. Network-optional: if the request fails
   (offline), emit an `info` finding saying the CVE check was skipped.
3. **Unused / undeclared deps** using graph 2.2:
   - declared but never imported → `suggestion` ("remove or document"),
     with an import-name↔package-name alias map (`pillow→PIL`,
     `beautifulsoup4→bs4`, `scikit-learn→sklearn`, …) to avoid false positives;
   - imported but not declared in any manifest → `high` issue ("works only by
     transitive luck").
4. **Hygiene**: unpinned specs (`*`, bare name, `>=` with no upper bound) in
   application (non-library) repos → `low`; `git+http://` sources → `high`.

### 3.3 `scanners/workflows.py`
YAML-parse workflow/container files (add `pyyaml` to requirements):
- **GitHub Actions:** third-party `uses:` not pinned to a full SHA (`@v4` =
  `medium`, `@main`/`@master` = `high`); `pull_request_target` combined with a
  checkout of `github.event.pull_request.head.*` (= critical, classic pwn
  request); `${{ github.event.*.title/body }}` interpolated into `run:` (script
  injection); missing top-level `permissions:` block (defaults to write-all on
  older settings); `secrets.*` echoed in `run:` steps; `continue-on-error` on
  security jobs.
- **Dockerfile:** `FROM :latest` or no tag; `ADD` with a URL; `curl | sh`;
  running as root with no `USER`; secrets in `ENV`/`ARG`; `apt-get upgrade`.
- **docker-compose:** `privileged: true`, host network, docker.sock mounts,
  plaintext credentials in `environment:`.

### 3.4 `scanners/architecture.py` (pure graph queries — this is where the graph pays off)
- **Circular imports:** `networkx.simple_cycles` over the `imports` edge
  subgraph → one finding per cycle (file list in evidence). Cap report at the
  20 shortest cycles.
- **Dead code:** definitions with `fan_in == 0`, not `is_test`, not
  `is_entrypoint` (2.4), not dunder/`main`, not exported in `__init__.py`/
  `__all__`. Emit as `suggestion` (static call graphs lie about dynamic
  dispatch — say so in the explanation).
- **God modules / hotspots:** top-N files by (defs count × total fan-in);
  functions > 100 lines; classes > 20 methods. `suggestion` severity.
- **Layering check (config-driven):** optional `audit.toml` with
  `layers = ["models", "services", "api"]`; report imports that point "upward".
  Skipped when not configured.
- **Duplicate logic:** reuse `EmbeddingIndex` (`embeddings.py`) — embed all
  chunks once, take pairwise cosine > 0.93 between chunks from *different*
  files, cluster, report the top clusters. Deep tier only (needs
  sentence-transformers).
- **Test coverage map:** for every non-test public function/route, reuse the
  `blast._covering_tests` reverse-BFS logic (factored out so blast and audit
  share it) to mark covered/uncovered; output a per-package coverage table and
  `high`-severity findings for **uncovered routes** specifically.

---

## 4. LLM layer (targeted, budgeted)

### 4.1 Risk-ranked file prioritization — `pr_review/audit.py:_rank_files`
LLM review of every file is unaffordable on real repos. Rank files by a
deterministic score and send only the top-K (K from the audit profile, e.g.
quick=0, standard=15, deep=40):

```
score(file) = 3·(# route/table/event defs)          # cg.routes()/tables()/events()
            + 2·sensitive_name_hit                   # reuse blast._SENSITIVE regex on path+names
            + 2·(secrets/scanner findings in file)   # scanner output feeds ranking
            + 1·normalized total fan-in of its defs
            + 1·entrypoint flag
```

### 4.2 Reuse the existing agents — new dossier builder
`BaseAgent.run` (`agents.py:234`) is dossier-agnostic; only the prompt framing
mentions a PR. Changes:
- `_user_prompt` gains a `mode` parameter (`"pr"` | `"audit"`): audit framing =
  "FULL file from the repository, no diff; review the file as it stands."
- New `context.build_audit_chunks(cg, file_path, token_budget)` — thin wrapper
  around the existing `make_file_chunks` with a synthetic
  `FileDiff(path, is_new=False, is_deleted=False, added_lines=set(), …)`
  (it already handles `added_lines=∅`: chunks simply show no `+` markers).
  The whole-file chunking logic at `context.py:335-403` is reused unchanged.
- Agents keep their tool loop (`get_callers`, `find_similar`, …) — repo-wide
  analysis benefits *more* from tools than PR review does.

### 4.3 One new agent
```python
class WorkflowSecurityAgent(BaseAgent):
    name = "workflow_security"
    category = "ci-cd security"
```
Receives raw workflow/Dockerfile text (they're small; no chunking) and reviews
what the deterministic rules can't catch (logic-level issues, e.g. a deploy job
triggered by unreviewed events). Registered in `AGENT_REGISTRY`
(`agents.py:391`) so profiles can toggle it.

The existing `SecurityAgent`, `ArchitectureAgent`, `PerformanceAgent`,
`TestCoverageAgent` run as-is over the ranked files. `CorrectnessAgent` is
**off** by default in audit mode (without a diff, "is this a regression?" has
no anchor — it produces noise).

### 4.4 Verifier
Reuse `review._verify` per file over LLM findings only (scanner findings are
exempt). Cap the dossier per finding-batch (see PR-review finding B3 — fix it
once, both modes benefit).

---

## 5. Orchestration & data model — new module `pr_review/audit.py`

```python
@dataclass
class AuditSection:
    key: str                      # "secrets" | "dependencies" | "workflows" | ...
    findings: List[Finding]
    stats: Dict[str, float]       # e.g. {"deps_total": 142, "deps_vulnerable": 3}

@dataclass
class AuditResult:
    sections: Dict[str, AuditSection]
    health_score: int             # 0-100, weighted like review._risk
    health_level: str             # low/medium/high/critical (risk, inverted for display)
    files_scanned: int
    files_llm_reviewed: List[str]
    profile_key: str
    duration_s: float

def run_audit(cg, root, nova, profile: AuditProfile,
              embed_index=None, progress_cb=None) -> AuditResult
```

Execution order inside `run_audit`:
1. scanners (secrets → dependencies → workflows → architecture), all local;
2. rank files (4.1) using scanner output;
3. LLM pass over top-K files (parallel chunk×agent, same executor as PR-review
   finding B1 — build it once in `review.py`, import it here);
4. verifier over LLM findings;
5. aggregate `health_score`: extend `review._risk`-style weighting with
   section weights (secrets findings weigh heaviest, suggestions weigh 0).

`Finding` gains two backward-compatible fields (default values, nothing else
changes): `source: str = "llm"` and `section: str = ""`.

### Audit profiles — extend `pr_review/profiles.py`
```python
@dataclass(frozen=True)
class AuditProfile:
    key: str                 # "scan" | "standard" | "deep"
    label: str
    llm_file_budget: int     # 0 / 15 / 40
    agent_keys: List[str]    # [] / [security, workflow_security] / all-but-correctness
    check_cves: bool         # OSV network call
    use_embeddings: bool     # duplicate-logic detection
    verify: bool
```
`scan` runs with **no AWS credentials at all** (pure scanners) — a genuinely
useful free tier and great for CI.

### Report — `audit.format_audit_report(result) -> str`
Markdown mirror of `review.format_report`: health score, per-section counts,
findings grouped by section then severity, dependency table (name / version /
CVEs / fix version), coverage table.

---

## 6. Streamlit UI changes (`streamlit_app.py`)

- `mode` radio (`:168`) gains a third option: **"Full codebase audit"**.
  Audit mode needs only repo + branch (default branch preselected) — no PR
  list. Reuses `download_source` + the graph cache (keyed per resolved SHA —
  depends on cache fix A3 in `CODE_REVIEW_FINDINGS.md`).
- Sidebar: when audit mode is active, show `AuditProfile` radio
  (Scan-only / Standard / Deep) instead of the PR depth radio; show the OSV
  toggle ("Check dependencies against OSV.dev — sends package names+versions
  to osv.dev") so the network call is explicit and consentful.
- Results: one tab per section —
  **Overview** (health score gauge, section counts, files scanned vs LLM-reviewed),
  **🔑 Secrets**, **📦 Dependencies** (sortable table + findings),
  **⚙️ Workflows**, **🏗 Architecture** (cycles rendered as `a → b → a` chains,
  dead-code list, duplicate clusters), **🧪 Coverage** (per-package table,
  uncovered routes flagged).
- Scanner findings get a `🔍 scanner` badge vs `🤖 model` (from
  `Finding.source`) so users know what's deterministic.
- Download button reuses `format_audit_report`.

---

## 7. File-by-file change list

| File | Change | Est. size |
|---|---|---|
| `pr_review/graph.py` | per-file index; extdep nodes; module-level calls; entrypoints; workflow/manifest/config file nodes; `workflows()/manifests()/external_imports()` accessors; TS grammar | ~150 lines modified/added |
| `pr_review/scanners/__init__.py` | new package, `run_all_scanners()` convenience | ~30 |
| `pr_review/scanners/secrets.py` | new | ~150 |
| `pr_review/scanners/dependencies.py` | new (manifest parsers + OSV client + alias map) | ~250 |
| `pr_review/scanners/workflows.py` | new (Actions/Docker/compose rules) | ~200 |
| `pr_review/scanners/architecture.py` | new (cycles, dead code, hotspots, dup-clusters, coverage map) | ~200 |
| `pr_review/audit.py` | new (ranking, orchestration, AuditResult, report) | ~250 |
| `pr_review/agents.py` | `Finding.source/section` fields; `WorkflowSecurityAgent`; `_user_prompt(mode=…)` | ~40 |
| `pr_review/profiles.py` | `AuditProfile` + 3 instances | ~50 |
| `pr_review/context.py` | `build_audit_chunks` wrapper | ~15 |
| `pr_review/blast.py` | factor `_covering_tests` core into a shared helper | ~10 |
| `pr_review/review.py` | extract the parallel chunk×agent executor so audit reuses it | ~30 |
| `streamlit_app.py` | audit mode UI + section tabs | ~180 |
| `requirements.txt` | add `pyyaml` (and the file itself — see hygiene finding D2) | — |
| `tests/` | scanner unit tests (regex fixtures, manifest parsing, workflow YAML fixtures, cycle detection on a toy graph) | ~300 |

**Dependency order:** graph changes (§2) → scanners (§3) → audit orchestration
(§5) → LLM layer (§4) → UI (§6). The scanners are independently shippable: a
`scan`-profile audit works before any LLM/agent work lands.

Recommended sequencing also folds in the prerequisite fixes from
`CODE_REVIEW_FINDINGS.md`: A3 (cache key), B1 (parallel executor), B2 (per-file
index), B3 (verifier cap) — all four are load-bearing for audit mode.

---

## 8. Costs, limits, failure modes

- **Token cost:** standard audit ≈ 15 files × ~2 chunks × 3 agents ≈ 90 LLM
  calls + verifier ≈ comparable to a Deep PR review of a large PR. Deep audit
  (40 files, 6 agents) is heavy — surface an estimated-call count in the UI
  before the run ("This will make ~N model calls").
- **Large repos:** `build_graph` caps at `max_files=5000`; the audit inherits
  it. Scanner pass streams file-by-file (no full-repo in memory).
- **Offline / no AWS:** `scan` profile fully functional; OSV failure degrades
  to an `info` finding; embeddings optional as today.
- **False-positive control:** secrets allowlist + entropy thresholds
  configurable via `audit.toml`; dead code always `suggestion`; every scanner
  finding carries the matched rule id in `evidence` so users can grep and
  suppress.

## 9. Verification

1. **Unit tests** per scanner with fixtures: a fake repo containing a planted
   AWS key, a `requirements.txt` with a known-CVE pin (e.g. `requests==2.5.0`),
   a workflow using `pull_request_target` + PR-head checkout, an import cycle,
   and an uncovered route — assert each produces exactly the expected finding.
2. **Self-audit:** run the audit against *this* repo — it should flag the
   `verify=False` TLS line (security scanner), the missing requirements file
   (dependency section), and zero-test coverage (coverage map). Good dogfood.
3. **UI walk:** Streamlit → audit mode → `scan` profile with no AWS creds
   (must complete); then `standard` with creds; confirm section tabs, badges,
   and the markdown download.
4. **OSV integration test** (network-marked, skipped offline): query a known
   vulnerable package and assert a CVE finding appears.
