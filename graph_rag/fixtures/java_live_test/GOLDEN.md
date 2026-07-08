# Golden dataset — `fixtures/java_live_test`

Hand-seeded ground truth for a first real Java end-to-end run of the
analyzer, mirroring `fixtures/live_test`'s Python golden set but exercising
Java-specific extraction (Spring annotations, constructor DI/`@Autowired`,
package/import-qualified resolution).

| # | Function | Expected category/subcategory | Notes |
|---|----------|-------------------------------|-------|
| 1 | `repository.OrderRepository.fetchOrder` | security/sql_injection | string-concat into `Statement.executeQuery` |
| 2 | `repository.OrderRepository.searchOrders` | security/sql_injection | string-concat into `Statement.executeQuery` |
| 3 | `repository.OrderRepository.saveOrder` | security/sql_injection | string-concat into `Statement.executeUpdate` |
| 4 | `repository.OrderRepository.deleteOrder` | security/sql_injection | string-concat into `Statement.executeUpdate`; also reachable two ways (see #9) |
| 5 | `service.OrderService.getOrder` | correctness/unhandled_empty | calls `rs.next()`/`rs.getString` without checking whether a row exists |
| 6 | `util.InventoryManager.reserveStock` | correctness/race_condition | read-modify-write on static shared `Map`, no lock |
| 7 | `config.AppConfig.loadConfig` | correctness/bad_error_handling | empty catch block swallows `IOException` |
| 8 | `resource.ReportResource.processReport` / `generateReport` | correctness/resource_double_release | `InputStream` closed in both functions across the call |
| 9 | `controller.OrderController.quickDeleteEndpoint` | architecture/layering_violation | controller calls `OrderRepository` directly, skipping `OrderService` |
| 10 | `controller.OrderController.adminDeleteOrderEndpoint` | security/missing_authorization | destructive delete with no auth check |
| 11 | `service.OrderService.notify` <-> `repository.OrderRepository.logNotification` (-> `OrderService.recordAudit`) | architecture/circular_architectural_dependency | deliberate service<->repository constructor + call cycle |

## Java-specific structural checks (not LLM findings — graph/resolver correctness)
- `OrderController`'s constructor params (`OrderService`, `OrderRepository`) should resolve to `AUTOWIRED` edges (single-constructor DI, no explicit `@Autowired` needed).
- `OrderController.getOrderEndpoint`'s call to `PathUtil.normalize` must resolve to `com.example.app.format.PathUtil` (the one it explicitly imports), NOT `com.example.app.util.PathUtil` (same simple name, different package, not imported) — the qualified-import-before-same-package precedence fixed in `resolver.py` this session.
- All 4 `@*Mapping`-annotated controller methods should become `Endpoint` nodes with `component_role=controller` / `endpoint_handler` roots for the architecture pass.

## Known limitation going in (not a bug in this fixture)
Agent B's deterministic Stage-1 taint pass (`taint.run_taint_pass`) is
Python-only (hardcoded `lang:'python'` + `get_parser("python")`), so the 4
SQL-injection bugs above will NOT be caught by the deterministic
graph-proven path for this Java fixture — only by Agent A's LLM pass
(`sql_injection` is in its subcategory list) if it catches them from raw
source alone. Agent B qualify/architecture are not similarly gated (qualify
has nothing to qualify since Stage 1 found nothing; architecture is
language-agnostic).
