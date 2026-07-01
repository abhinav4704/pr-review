# GraphRAG Implementation Summary and Next Steps

## Scope Covered
This document summarizes the implementation work completed in this session and proposes prioritized next changes.

## What Was Implemented

### 1) Dependency and relation coverage improvements
- Added DEFINES as additive semantic ownership in both Python and Java extractors.
- Added PASSES end-to-end:
  - extractor emission (argument-name hints at call sites)
  - model payload support (`arg_names`)
  - resolver handling and edge persistence
- Added REFERENCES fallback behavior in resolver for unresolved call-like references.

### 2) Event and auth/policy edges
- Added schema support for:
  - node labels: Event, Policy
  - edge types: EMITS_EVENT, CONSUMES_EVENT, REQUIRES_AUTH, ENFORCES_POLICY
- Added Python extractor heuristics for:
  - event emit/consume call patterns
  - event-consumer decorators
  - auth/policy decorators
- Added Java extractor heuristics for:
  - event emit/consume invocation patterns
  - listener annotations (consumer topics)
  - auth/policy annotations
- Added resolver materialization for:
  - Event nodes from event/topic refs
  - Policy nodes from auth/policy refs
  - corresponding EMITS_EVENT/CONSUMES_EVENT/REQUIRES_AUTH/ENFORCES_POLICY edges

### 3) Architecture derivation layer
- Added deterministic role classification metadata to nodes:
  - `component_role`
  - `role_source`
  - `role_confidence`
- Added module ownership metadata:
  - node property `module_id`
  - BELONGS_TO edges from owned nodes to derived Module nodes
- Added USES derivation:
  - component-level USES aggregated from low-level dependency edges
  - module-level USES aggregated from component USES

## Files Changed
- graph_rag/schema.py
- graph_rag/models.py
- graph_rag/resolver.py
- graph_rag/extractors/python.py
- graph_rag/extractors/java.py
- graph_rag/pipeline.py

## Validation Performed
- Static diagnostics: no errors in modified files.
- Resolver smoke test: `python measure_coverage.py samples --repo samples` succeeded.
- Full index command attempted:
  - `python -m graph_rag.cli index samples --repo samples --no-scip --no-wipe`
  - failed due to Neo4j connectivity (localhost:7687 unavailable), not code-level errors.

## Design Choices and Rationale

### REFERENCES as fallback only
- Implemented as a weaker, deterministic fallback when call-like resolution fails.
- Preserves precision-first behavior for CALLS/PASSES while improving recall.

### Event/auth edges as additive
- Added without replacing existing deterministic graph facts.
- Uses explicit confidence/provenance strategy in resolver for traceability.

### USES as derived, not extracted
- USES is an architectural summary relation.
- Derived from existing low-level edges to reduce noise and improve PR-level blast radius reasoning.

## Known Gaps / Risks
- Event/auth heuristics are rule-based and may need repository-specific tuning for best precision.
- Sample dataset currently does not visibly exercise all newly added relations in coverage output.
- Java precise call resolution still depends on heuristic behavior when SCIP-like precision is unavailable.

## Suggested Next Changes (Priority Order)

### Priority 1: Validation fixtures and regression checks ✅ DONE
- Added compact synthetic fixtures under `fixtures/` (`orders/service.py`,
  `billing/gateway.py`, `auth/SecuredController.java`) that explicitly trigger
  EMITS_EVENT, CONSUMES_EVENT, REQUIRES_AUTH, ENFORCES_POLICY, PASSES, DEFINES,
  BELONGS_TO, USES (incl. cross-module `orders -> billing`), plus Event/Policy/
  Module node materialization and controller role tagging.
- Added `validate_fixtures.py`: replays the DB-free pipeline (heuristic, no SCIP)
  step-for-step over `fixtures/` and asserts each edge/node still fires, with a
  graph-validity check. Exit 1 on regression. **19/19 checks pass** (after the
  Priority 3/4 work below).
- **Note — `@PreAuthorize` double-tags.** Its name contains the substring "auth",
  so Java's `_java_auth_policy_specs` emits both REQUIRES_AUTH and ENFORCES_POLICY
  for it. Harmless (additive), but worth a substring tightening if precision matters.

### Priority 1b: REFERENCES made reachable (precision-aligned) ✅ DONE
- Original finding: REFERENCES was unreachable via extraction — extractors emit a
  *simple* call name, and `_fallback_reference_candidates` only fires on a *dotted*
  target. It's a genuinely useful recall fallback for PR review, so rather than
  drop it we made it fire correctly.
- `resolver.py`: a CALLS on an **unknown receiver** (`recv` set, not `self`/`cls`)
  that matched only by the weakest global-name strategy is now **demoted from CALLS
  to a weak REFERENCES** symbol-use edge — it likely targets an external object that
  merely shares a method name, so this *improves CALLS precision* while keeping the
  recall signal. Bare calls and known-receiver calls are unchanged; coverage now
  accounts the site under REFERENCES.
- Verified no regression on `samples` (identical CALLS coverage); fixture
  `audit/log.py` + `tracer.stamp(...)` locks the new path.

### Priority 2: AUTOWIRED deterministic support (Java Spring first) — NEXT
- Extract constructor/field/parameter injection signals.
- Resolve injected type targets deterministically.
- Add AUTOWIRED edges with clear provenance.
- (scip-java precise Java CALLS deferred: needs Coursier + a buildable Maven/Gradle
  project; no such toolchain/project present yet.)

### Priority 3: Improve role classification determinism ✅ DONE
- Role rules moved into an explicit, ordered mapping `_CLASS_ROLE_RULES` in
  `pipeline.py` (annotation > name-suffix > package), editable in one place.
- `_classify_roles` now returns a per-(role, source) diagnostics counter, surfaced
  on `IndexResult.roles`, so assignment quality (HIGH annotation vs LOW package
  fallback) is observable.

### Priority 4: Strengthen module boundary modeling ✅ DONE
- `_derive_module_ownership_and_uses` takes `module_roots` (explicit module-root
  prefixes, longest-match-wins) and `module_root_depth` (how many leading
  package/path segments form a module key; default 1 = old behaviour).
- Component-level USES now carry a `component_aggregate:cross_module` /
  `:intra_module` strategy tag so boundary-crossing deps (blast-radius signal) are
  queryable without recomputation.

### Priority 5: Re-export support when TS/JS is introduced
- Add RE_EXPORTS extraction/resolution once TypeScript/JavaScript indexing is in scope.

## Suggested Operational Checklist
1. Start Neo4j and re-run full index command.
2. Confirm Module, BELONGS_TO, and USES counts in DB.
3. Run semantic identity generation on top of updated graph.
4. Add synthetic fixtures and lock expected edge counts.

## Summary
The graph is now materially stronger for PR review use-cases: better dependency recall, event/auth awareness, role/module structure, and architectural USES derivation. The highest-value next step is to formalize validation fixtures and then add deterministic AUTOWIRED support.