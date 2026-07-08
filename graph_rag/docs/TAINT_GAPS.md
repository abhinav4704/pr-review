# Taint Engine (Group B) — Known Gaps

Status snapshot as of 2026-07-03, after the logic-fix + sink-catalog-expansion
pass on `taint.py`/`pipeline.py`. These gaps are **not urgent for current use**
— recall/precision are already meaningfully improved — but are recorded here
so a future pass knows exactly what's left instead of rediscovering it.

## Composition / graph logic

- **Return-flow is same-function-only, and conservative, not proven.**
  `x = foo(tainted_arg)` marks `x` tainted within the SAME function as an
  over-approximation. We never look inside `foo` to confirm it actually
  returns something derived from its argument. Fine for recall, costs some
  precision (false positives).
- **Dynamic `*args`/`**kwargs` splats are unmappable.** Literal splats
  (`foo(*[x])`, `foo(**{"id": x})`) are now mapped precisely. A splat wrapping
  a variable built elsewhere (`foo(**config)`) has no statically-knowable
  target parameter — permanent blind spot short of real interprocedural
  constant tracking.
- **Callee resolution falls back to bare-name matching** when no resolved
  `CALLS` edge narrows it. Can still over-match when multiple functions share
  a name and the call site wasn't resolved with high confidence.
- **No control-flow/branch sensitivity.** A sanitizer call in one `if` branch
  and a sink in the `else` branch are not distinguished — the engine has no
  CFG, so it can't tell mutually-exclusive paths apart. Needs a real
  control-flow model to fix, not a small patch.
- **No field/container-level taint.** A dict/object with one tainted key is
  treated as fully tainted (or fully clean), not per-field.

## Sink / source catalog (open-ended by nature)

- Still missing: header/response-splitting, SSTI beyond
  `render_template_string`/`from_string`, GraphQL resolver sinks, gRPC/message
  -queue consumers as taint sources, custom ORM/DAO wrapper methods with
  non-obvious names.
- `command_injection` (`call`) and `ldap_injection` (`search`) have weak/no
  receiver-hint narrowing — some false-positive risk on generic method names
  reused for unrelated purposes.
- Taint sourcing is **parameter-only**. A tainted module-level global set
  elsewhere (e.g. from an env var or request context in another function)
  is never seeded as a taint origin, even though global reads/writes are
  already tracked structurally elsewhere in the graph.

## Endpoint / source classification

- `EXPOSES` edge detection (which framework decorators/route patterns are
  recognized) hasn't been broadened. The controller-class fallback
  (`component_role == "controller"` → `endpoint_handler`, MEDIUM confidence)
  helps, but entry points with no owning "controller"-tagged class and no
  recognized decorator (blueprints, class-based views, GraphQL/gRPC handlers)
  still won't seed a taint walk at all.

## Sanitizer accuracy

- Still heuristic: name-regex hints (`sanitiz|escape|clean|validate|...`) +
  LLM-tagged candidates. A custom sanitizer with a non-obvious name that
  wasn't flagged as a candidate silently fails to suppress a finding (or,
  conversely, a function that merely *looks* like a sanitizer by name but
  isn't could suppress a real one).
- No deterministic sanitizer-pattern table exists yet (analogous to
  `SINK_PATTERNS`) for well-known library sanitizers (`bleach.clean`,
  `markupsafe.escape`, `shlex.quote`, parameterized-query binding).

## Java

- On hold intentionally. Zero taint coverage for Java right now — Java
  `CALLS` resolution is still heuristic and gates behind a resolution-
  coverage metric before taint should be trusted there (see `taint.py`
  module docstring). Plan: once Python is solid and stable in real use,
  port the same approach (transfer-function extraction, sink/sanitizer
  catalogs, composition) to the Java extractor.

## Possible future direction: LLM "qualify" pass

A Stage-3 pass that takes `enumerate_taint_paths()` output, pulls the real
source of every function in the chain, and asks an LLM "given this exact
code, is this actually reachable/unsanitized?" would fix most of the
**precision** gaps above (branch-sensitivity, sanitizer accuracy, return-flow
correctness) — but it is a precision filter on top of what the deterministic
engine already found, and cannot fix any of the **recall** gaps (sink catalog
completeness, Java coverage, unmapped dynamic splats, wrong callee picked).
Not built yet — noted here as the natural next step if more precision is
needed later.
