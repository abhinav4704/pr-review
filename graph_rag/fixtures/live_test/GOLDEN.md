# Golden dataset — `fixtures/live_test`

Hand-curated "ground truth" for the bugs deliberately seeded in this fixture.
Compare the pipeline's live output (e.g. `out_analyze_live2.json`) against this
list to judge precision/recall — not against a formal spec, since there isn't one.

Legend: **TP** = real bug you'd want a review tool to catch. Findings not on
this list are either (a) reasonable bonus catches, or (b) noise — judge case by case.

| # | Function | Expected category/subcategory | Notes |
|---|----------|-------------------------------|-------|
| 1 | `repository.OrderRepository.fetch_order` | security/sql_injection | f-string into `conn.execute`, reachable from `get_order_endpoint` |
| 2 | `repository.OrderRepository.search` | security/sql_injection | reachable from `search_orders_endpoint` (sanitized upstream by `normalize_term`, but the sink itself is still unsafe if called elsewhere) |
| 3 | `repository.OrderRepository.delete_order` | security/sql_injection | reachable from `quick_delete_endpoint` (also a layering violation, see #9) |
| 4 | `repository.OrderRepository.save_order` | security/sql_injection | reachable from `place_order_endpoint` |
| 5 | `services.OrderService.get_order` | correctness/unhandled_empty | indexes into `fetch_order`'s result without checking for empty list |
| 6 | `inventory.reserve_stock` | correctness/race_condition | read-modify-write on shared dict with no lock |
| 7 | `config.load_config` | correctness/bad_error_handling | bare `except` swallows real errors |
| 8 | `resource.process_report` / `resource.generate_report` | correctness/resource_double_release | handle closed twice across the two functions |
| 9 | `controllers.quick_delete_endpoint` | architecture/layering_violation | calls `OrderRepository` directly, skipping `OrderService` |
| 10 | `controllers.admin_delete_order_endpoint` | security/missing_authorization | deletes with no auth check, plus its own direct SQLi |
| 11 | `services.OrderService.notify` <-> `repository.OrderRepository.log_notification` | architecture/circular_architectural_dependency | deliberate service<->repository call cycle |

## Deliberately CLEAN (should NOT be flagged as buggy)
- `controllers.search_orders_endpoint` -> `search_orders` -> `normalize_term` -> `search` — sanitized chain, no SQLi.
- `controllers.place_order_endpoint` -> `reserve_stock` + `OrderService.place_order` — normal flow (reserve_stock's race condition is bug #6, not this chain itself).
- `controllers.get_order_endpoint` — clean itself; the bug (#5) is downstream in `get_order`.

## How to score a run
- **Recall** = (# of the 11 above actually reported, correct function + correct subcategory) / 11.
- **Precision noise to watch for**: duplicate re-flags of the same bug under a different category (e.g. Stage 1 `security/sql_injection` vs Agent1 `correctness/sql_injection` on the same function/line — currently NOT deduped, since dedupe keys on category+subcategory+line), "no known callers" style non-findings, stub helpers (`_Router.get`, `.deco`) getting analyzed at all, and severity scores that don't differentiate (everything landing on the same default value).
