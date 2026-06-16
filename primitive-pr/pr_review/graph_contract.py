"""Canonical graph contract helpers.

This module keeps legacy primitive-pr edge fields (type/confidence) compatible
with the canonical relation/confidence model used by Graphify-style graphs.
"""

from __future__ import annotations

from typing import Any, Mapping


# Canonical relation names shared by PR mode and full-repo mode.
RELATIONS = {
    "calls",
    "imports",
    "references",
    "inherits",
    "overrides",
    "instantiates",
    "decorates",
    "implements",
    "extends",
    "uses",
    "re_exports",
    "defines",
}


# Higher weight means the relation is a stronger signal for impact traversal.
RELATION_WEIGHTS = {
    "calls": 1.0,
    "instantiates": 0.95,
    "overrides": 0.9,
    "inherits": 0.85,
    "implements": 0.8,
    "extends": 0.8,
    "imports": 0.7,
    "references": 0.65,
    "uses": 0.6,
    "re_exports": 0.55,
    "decorates": 0.5,
    "defines": 0.4,
}


def edge_relation(data: Mapping[str, Any], default: str = "") -> str:
    """Return canonical edge relation from either relation or legacy type."""
    relation = str(data.get("relation") or data.get("type") or default)
    return relation.strip().lower()


def normalize_confidence(value: Any) -> str:
    """Map mixed confidence labels to canonical EXTRACTED/INFERRED/AMBIGUOUS."""
    raw = str(value or "").strip().lower()
    if raw in {"ambiguous", "maybe", "guess"}:
        return "AMBIGUOUS"
    if raw in {"unique", "same_file", "extracted", "exact"}:
        return "EXTRACTED"
    if raw in {"inferred", "heuristic"}:
        return "INFERRED"
    # Legacy/unknown confidence labels are treated as inferred rather than exact.
    return "INFERRED"


def confidence_score(value: Any) -> float:
    """Numeric confidence score used for ranking ties."""
    kind = normalize_confidence(value)
    return {
        "EXTRACTED": 1.0,
        "INFERRED": 0.6,
        "AMBIGUOUS": 0.2,
    }[kind]


def is_ambiguous_confidence(value: Any) -> bool:
    return normalize_confidence(value) == "AMBIGUOUS"


def is_same_file_confidence(value: Any) -> bool:
    return str(value or "").strip().lower() == "same_file"


def relation_weight(relation: str) -> float:
    return RELATION_WEIGHTS.get(str(relation or "").strip().lower(), 0.5)
