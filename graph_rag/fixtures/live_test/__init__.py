"""live_test — a small, deliberately interconnected fixture repo mixing a few
clean layered chains with real bugs, used to test whether Stage 2 (Agent 1/2)
and the architecture pass actually produce findings when run with a live LLM.

See individual modules' docstrings for what's seeded where:
  controllers.py  — endpoints; one clean chain, one layering violation
                    (quick_delete_endpoint), one missing-auth
                    (admin_delete_order_endpoint)
  services.py     — unhandled-empty bug (get_order); a sanitizer
                    (normalize_term); half of a deliberate service<->repository cycle
  repository.py   — raw-SQL sinks reached by every endpoint above; other half
                    of the cycle (log_notification)
  inventory.py    — race condition (reserve_stock)
  config.py       — bad error handling (load_config)
  resource.py     — resource double-release (generate_report)
"""
