# Seeded eval branch — ground truth

A starter seeded branch (v1 — plan.md §6 calls for 12-15; this is 7, chosen to
cover every bug class the current build (taint.py Stage 1 + Agent 1/2 design)
can actually attempt). Extend this file and the fixtures together as more
Stage 1 passes (authz/stored-taint/concurrency/perf) get built.

| # | Class                     | File            | Function              | Expected finder            |
|---|---------------------------|------------------|------------------------|-----------------------------|
| 1 | sql_injection (multi-hop) | db.py:~20        | fetch_rows             | Stage 1 taint composition   |
| 2 | command_injection (multi-hop) | shell.py:~15 | run_diagnostic          | Stage 1 taint composition   |
| 3 | correctness/unhandled_empty | service.py:~15 | get_first_result       | Agent 1                     |
| 4 | correctness/resource_double_release | resource.py | run_job (via process) | Agent 2 (cross-function)    |
| 5 | correctness/race_condition (RMW/TOCTOU) | inventory.py | reserve_stock  | Agent 1                     |
| 6 | correctness/bad_error_handling | errors.py:~10 | parse_config           | Agent 1                     |
| 7 | security/missing_authorization | admin.py:~10 | delete_user_endpoint  | Agent 1 or Agent 2          |

## Clean control (should NOT fire)
- `db.py:export_report` — same sink function family as #1 but called with a
  hardcoded query string; taint composition must NOT flag it (tests that the
  qualify step / taint source-tracing doesn't over-fire on untainted callers).
- `service.py:normalize` — deliberately does nothing but `.strip().lower()`;
  the sanitizer-tagging LLM pass must NOT mark it as sanitizing anything.

## Not yet seeded (needs a Stage 1 pass that doesn't exist yet)
- Stored/second-order XSS — needs the stored-taint pass (WRITES-reachable +
  READS-flows-to-sink), not built.
- Missing-auth as a *deterministic* finding — needs the authz-candidate Stage 1
  pass, not built; #7 above currently relies on Agent 1/2 judgment only.
