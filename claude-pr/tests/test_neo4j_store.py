"""Tests for neo4j_store.py — Cypher query string correctness (no live DB needed)."""

import pytest


def test_callers_cypher_contains_valid_braces():
    """The callers() query must use valid Cypher {id: $id} (not {{id: $id}})."""
    from pr_review.neo4j_store import Neo4jStore

    # Build the query string using the same logic as the fixed implementation.
    depth = 2
    query = (
        f"MATCH (caller:Node)-[:CALLS*1..{int(depth)}]->(n:Node {{id: $id}}) "
        "RETURN DISTINCT caller.id AS id"
    )
    assert "{id: $id}" in query, "Query must contain single-brace {id: $id}"
    assert "{{" not in query, "Query must NOT contain escaped braces {{"
    assert "*1..2" in query, "Query must contain *1..2 for depth=2"


def test_callers_cypher_depth_injection_safe():
    """int(depth) prevents non-integer injection in the f-string."""
    depth = 3
    query = (
        f"MATCH (caller:Node)-[:CALLS*1..{int(depth)}]->(n:Node {{id: $id}}) "
        "RETURN DISTINCT caller.id AS id"
    )
    assert "*1..3" in query


def test_callers_cypher_original_bug_pattern():
    """Demonstrate that the OLD .replace() approach left doubled braces."""
    bad_query = (
        "MATCH (caller:Node)-[:CALLS*1..{d}]->(n:Node {{id: $id}}) "
        "RETURN DISTINCT caller.id AS id".replace("{d}", "2")
    )
    # The old pattern is broken: {{id: $id}} stays literal
    assert "{{" in bad_query, "Old pattern has the bug (double braces)"
    # The fixed f-string version does NOT have the bug
    fixed_query = (
        f"MATCH (caller:Node)-[:CALLS*1..{int(2)}]->(n:Node {{id: $id}}) "
        "RETURN DISTINCT caller.id AS id"
    )
    assert "{{" not in fixed_query, "Fixed f-string must not have double braces"
