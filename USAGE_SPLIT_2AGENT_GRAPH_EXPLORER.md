# Usage Split: prompt_test_frontend vs Graph Explorer

## Purpose
This file separates runtime ownership so cleanup can be done safely without breaking the prompt_test_frontend flow.

## A) Files used by prompt_test_frontend flow
Entry UI:
- primitive-pr/prompt_test_frontend.py

Primary orchestration:
- primitive-pr/pr_review/two_agent_review.py

Prompt/chain builders:
- primitive-pr/pr_review/prompt_builder.py

Graph build for this flow (in-memory):
- primitive-pr/pr_review/graph.py

LLM wiring and finding model:
- primitive-pr/pr_review/review_llm.py
- primitive-pr/pr_review/findings.py
- primitive-pr/pr_review/diff.py
- primitive-pr/pr_review/pr_passes.py (used by two_agent_review for pass_whole_file)

### prompt_test_frontend and Neo4j
- prompt_test_frontend path does not require Neo4jStore.
- It uses in-memory build_graph output and runs run_two_agent_review directly.

## B) Files used by Graph Explorer flow
Entry UI:
- primitive-pr/graph_explorer.py

Neo4j persistence/query layer:
- primitive-pr/pr_review/neo4j_store.py

Review pipeline used by graph_explorer:
- primitive-pr/pr_review/pr_passes.py
- primitive-pr/pr_review/synthesis.py

Graph build:
- primitive-pr/pr_review/graph.py

### Graph Explorer and Neo4j
- graph_explorer constructs Neo4jStore and pushes graphs to Neo4j.
- If Neo4jStore path is removed, graph_explorer functionality is reduced/broken.

## C) Shared files (used by both paths)
- primitive-pr/pr_review/graph.py
- primitive-pr/pr_review/pr_passes.py
- primitive-pr/pr_review/findings.py
- primitive-pr/pr_review/diff.py

## D) graphify_adapter: are we using it?
Yes, conditionally.

Current behavior in primitive-pr/pr_review/graph.py:
- backend == "vendored" is remapped to "graphify".
- backend in {"graphify", "auto"} attempts try_build_with_graphify from primitive-pr/pr_review/graphify_adapter.py.
- If graphify path is unavailable and backend is "auto", code falls back to primitive extraction.
- If backend is explicitly "graphify" and unavailable, it raises an error.

Implication:
- If you run backend="primitive", graphify_adapter is not used.
- If you run backend="vendored" or backend="auto" (or "graphify"), graphify_adapter may be used.

## E) Safe cleanup guidance
Safe for prompt_test_frontend-only runtime:
- Remove graph_explorer-only UI code after confirming no imports from prompt_test_frontend/two_agent_review.

Not safe if you still want Graph Explorer + Neo4j features:
- Deleting primitive-pr/pr_review/neo4j_store.py
- Deleting graph_explorer Neo4j wiring
- Deleting shared files listed above

## F) Decision checklist before deletion
1. Keep prompt_test_frontend working: preserve prompt_test_frontend + two_agent_review + shared files.
2. Keep future Neo4j graph explorer ability: preserve graph_explorer + neo4j_store + pr_passes/synthesis wiring.
3. Remove graphify_adapter only after removing vendored/graphify/auto dispatch in graph.py and validating no references remain.
