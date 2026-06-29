Next Steps - Code Knowledge Graph



Current Status



The deterministic structural graph is largely complete and operational.



Next Steps - Code Knowledge Graph

## Current Status

The deterministic structural graph is largely complete and operational.

### Completed

- Repository discovery
- Multi-language parsing (Java + Python)
- RawRef extraction
- Heuristic symbol resolution
- Neo4j storage
- Stable IDs
- Rich metadata
- Provenance
- Structural graph
- Type graph
- Dockerized pipeline
- CLI + Web UI

Current graph successfully indexes real repositories (for example, Sail).

---

## Deterministic Completion Plan (Finish First)

We will complete all deterministic milestones (M3-M7), run quality gates, then move to Phase 2.

### M3 - Symbol Resolution

Goal: compiler-grade symbol resolution.

Tasks:

- Integrate SCIP for Python end-to-end and make activation visible in run output.
- Improve Java symbol resolution quality.
- Resolve identifiers across:
	- Definitions
	- References
	- Imports
	- Exports
	- Aliases
	- Scopes

Definition of Done:

- Python resolver reports SCIP coverage and overrides in each indexing run.
- Java unresolved and ambiguous references are reduced against baseline repositories.
- Resolution metrics are exported per language and relation type.

### M4 - Program Relationships

Add deterministic relationships:

- READS
- WRITES
- DECLARES
- OVERRIDES
- USES_TYPE
- THROWS
- CATCHES

Definition of Done:

- Relationship extraction is deterministic and reproducible.
- Each edge stores provenance and source range.
- New relationships are queryable from CLI/Web UI and persisted in Neo4j.

### M5 - Static Metrics

Compute compiler-derived metrics.

Initial metric set:

- Cyclomatic Complexity
- Fan In
- Fan Out
- Lines of Code
- Branch Count
- Loop Count
- Recursion
- Purity (optional)

Definition of Done:

- Metrics are generated per function/method.
- Metrics are versioned in schema and available through graph queries.
- Regression checks verify stable values across unchanged code.

### M6 - Canonical Code Model

Finalize language-independent intermediate representation.

Flow:

Language Parser
-> Canonical IR
-> Knowledge Graph

Definition of Done:

- Java and Python map to one canonical schema.
- Language-specific details are preserved as optional extensions.
- IR is documented with examples and invariants.

### M7 - Validation Suite

Automatically verify graph correctness.

Checks:

- No dangling edges
- No duplicate IDs
- Valid source ranges
- Resolver accuracy
- Coverage statistics
- Regression repositories

Definition of Done:

- Every indexing run ends with a machine-readable validation report.
- CI fails on critical validation errors.
- Baseline repositories pass deterministically.

---

## Deterministic Freeze Gate

Stop here after M3-M7 are complete.

Deterministic layer is considered frozen once:

- Validation suite is green on baseline repositories.
- Coverage and accuracy targets are met.
- No open critical deterministic defects remain.

No additional deterministic features should be added unless they unlock a concrete capability.

---

## Phase 2 - Semantic Knowledge (Starts After Freeze Gate)

Generate structured semantic artifacts only after deterministic completion.

### Function

- Semantic Identity
- Implementation Flow
- Responsibilities
- Key Concepts
- Side Effects
- Business Rules
- Security Notes

### Class

- Identity
- Responsibility
- Collaborators
- Public API
- Owned State

### Package

- Purpose
- Responsibilities
- Dependencies

### Module

- Purpose
- Architecture
- Entry Points
- Data Flow

### Repository

- Architecture
- Features
- Major Modules
- Tech Stack
- High-level Flow

Generation order must be bottom-up:

Functions
-> Classes
-> Packages
-> Modules
-> Repository

---

## Phase 3 - Knowledge API

Build an abstraction over Neo4j.

Examples:

- get_function(...)
- get_class(...)
- get_callers(...)
- get_module(...)
- expand_context(...)
- find_related_entities(...)

Agents should never query Neo4j directly.

---

## Phase 4 - Expert Reviewers

Build specialized reviewers before a fully autonomous agent.

Order:

1. Architecture Reviewer
2. Security Reviewer
3. Performance Reviewer
4. Maintainability Reviewer

Each reviewer uses:

- Structural Graph
- Semantic Knowledge
- Raw Code

---

## Phase 5 - Investigation Engine

Introduce an iterative reasoning loop:

Question
-> Plan
-> Retrieve
-> Reason
-> Collect More Evidence
-> Repeat
-> Final Report

The investigation engine should be generic.

Different expert reviewers should only provide:

- Retrieval strategy
- Reasoning policy
- Validation rules

---

## Phase 6 - Advanced Static Analysis

Move advanced compiler analyses here.

Includes:

- CFG
- DFG
- PDG
- Alias Analysis
- Security Data Flow
- Advanced Program Analysis

These are intentionally not on the critical path.

---

## Immediate Execution Focus

Now: finish deterministic milestones M3-M7.

Next: run freeze-gate validation.

Then: start Phase 2 semantic knowledge.