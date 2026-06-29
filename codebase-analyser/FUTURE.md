# Future Roadmap: CLI and CI (Deferred)

This file documents future automation only.
Current implementation remains manual-first through Streamlit and module execution.

## Why deferred

- Current workflow is manual by preference (select repo/branch and inspect graph-driven findings).
- Early value is deterministic audit quality, not automation plumbing.
- CI/CLI are useful later when repetition and team scale increase.

## Future CLI Plan

## Stage CLI-1: Minimal command runner

Goal:
- Run deterministic audit from terminal for a repo path.

Scope:
- Command accepts: repo path, output dir, depth, top-n.
- Emits markdown/json reports identical to Streamlit backend output.

Success criteria:
- CLI output matches manual run for same repository/settings.

## Stage CLI-2: Feature flags and profiles

Goal:
- Add scanner/LLM controls and run presets.

Scope:
- Flags for scanner enablement and LLM budget.
- Presets: quick, standard, deep.
- Stable exit codes (0 success, non-zero failure types).

Success criteria:
- Deterministic behavior and script-friendly invocation.

## Stage CLI-3: DX hardening

Goal:
- Make CLI easy for contributors.

Scope:
- Structured logs and timing metrics.
- Helpful error messages and usage docs.

Success criteria:
- New contributor can run without reading source internals.

## Future CI Plan

## Stage CI-1: Tests-only pipeline

Goal:
- Protect base quality.

Scope:
- Trigger on PR and push.
- Install dependencies and run pytest.

Success criteria:
- Stable green test pipeline on default branch.

## Stage CI-2: Static quality gates

Goal:
- Enforce style and static correctness.

Scope:
- Add formatting/lint checks.
- Add type checks if typing coverage is adequate.

Success criteria:
- Low flakiness and clear actionable failures.

## Stage CI-3: Analyzer smoke run

Goal:
- Validate audit execution itself in CI.

Scope:
- Run deterministic audit on fixture repository.
- Validate report artifacts exist and follow schema.
- Keep LLM disabled by default.

Success criteria:
- Reproducible smoke outputs across runs.

## Stage CI-4: Optional scheduled scans

Goal:
- Add periodic codebase health snapshots.

Scope:
- Scheduled or manual workflow dispatch.
- Artifact upload for report review.
- Strict runtime and budget limits.

Success criteria:
- Reliable scheduled execution without blocking contributor velocity.

## Readiness Checklist Before CLI/CI Start

- Deterministic finding schema is stable.
- Phase 1.5 and Phase 2 tests are in place.
- Manual run is reliable on representative repositories.
- Export schema versioning is finalized.

## Non-goals for automation rollout

- Do not replace Streamlit/manual workflow.
- Do not force LLM in CI.
- Do not couple CI success to external API availability without fallbacks.
