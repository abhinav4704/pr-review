# What each agent actually sees

Quick reference for the LLM-facing input bundle per agent. "Raw" = actual source
code text. "Identity/shape" = fqn, signature, docstring, role, `implementation_flow`
— never the function body.

## Agent 1 — correctness (`agents.py::run_agent1`, Group A)
- **Self**: raw source of the function under review.
- **Callees**: raw source of each direct callee (capped at `_FANOUT_GUARD=40`).
- **Layer-3 facts**: `taint_json` for self (if it reaches a known sink) + shared
  instance/class fields it reads/writes (from real `READS`/`WRITES` edges).
- **Why raw for callees**: correctness needs detail — e.g. "does it trust what
  it gets back?" requires seeing the callee's actual body.
- **Never sees**: callers, architecture-level shape info.

## Agent 2 — impact/breakage (`agents.py::run_agent2`, Group C)
- **Self**: raw source of the function under review.
- **Callers**: NOT raw — only identity/shape (fqn, signature, docstring,
  identity, `component_role`), capped at `_FANOUT_GUARD=40`.
- **Layer-3 facts**: `taint_json` for self.
- **Why shape-only for callers**: impact only needs to know what a caller
  expects (its contract), not how the caller is implemented.
- **Skips entirely** if the function has no known callers (avoids "no callers"
  noise findings).

## Agent 3 — taint qualify (`taint.py::qualify_taint_finding`, Group B, new)
- **Never raw source**, for any hop.
- Per finding: the full source→sink fqn chain (`path`), and for **every
  function in that chain** — its `component_role` + `implementation_flow`
  (an ordered list of short natural-language step summaries, generated/cached
  once via `generate_flows`, same mechanism Agent 4 uses).
- **Also sees**: `vuln_class`, claimed sink fqn/file/line.
- **Purpose**: precision filter on top of the deterministic graph-proven
  finding — decide `true_positive` / `false_positive` / `needs_more_context`
  purely from what the flow summaries show, not from re-reading code.

## Agent 4 — architecture deep-dive (`architecture.py::analyze_shape`, Group D)
- **Never raw source.**
- Stage 1 (`flag_risky_shapes`, cheap LLM call): each distinct call-chain
  **shape** (sequence of `component_role`s) plus up to 2 example chains
  rendered as `fqn (short docstring first line) -> ...` — identity + doc
  only, still no bodies.
- Stage 2 (`analyze_shape`, only for shapes flagged risky in Stage 1): for
  each function in the flagged chain — fqn, `component_role`, and
  `implementation_flow` (same cached flow mechanism as Agent 3).
- **Purpose**: judge layering violations, missing authorization, and other
  architecture-level issues from structure + intent summaries, not code.

## Summary table

| Agent | Group | Sees raw code? | What it sees instead |
|---|---|---|---|
| 1 | A (self) | self + callees (raw) | taint facts, shared-field reads/writes |
| 2 | C (impact) | self only (raw) | callers as identity/signature/docstring shape |
| 3 | B (taint qualify) | never | chain fqns + role + `implementation_flow` per hop |
| 4 | D (architecture) | never | shape catalogue (Stage 1) / chain fqns + role + `implementation_flow` (Stage 2) |
