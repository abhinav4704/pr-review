"""Seeded eval branch (v1 — a starter set, not the full 12-15 in plan.md §6 yet).

Ground truth for what SHOULD be found, by file:line — see SEEDED_BUGS.md in
this directory. Covers: 2 multi-hop injections (taint.py, Stage 1), 1 intra-fn
correctness bug, 1 cross-file resource bug, 1 RMW race, 1 bad-error-handling,
1 missing-auth (the latter four need Agent 1/2 judgment — no deterministic
authz/concurrency Stage 1 pass exists yet, see plan.md §9 step 6).
"""
from .db import export_report, fetch_rows
from .service import get_first_result, run_search

__all__ = ["fetch_rows", "export_report", "run_search", "get_first_result"]
