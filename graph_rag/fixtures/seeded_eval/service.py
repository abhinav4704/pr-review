"""Business logic layer for the seeded eval branch."""
from .db import fetch_rows


def normalize(term):
    """Deliberately NOT a sanitizer — only case/whitespace normalization.
    The sanitizer-tagging LLM pass must not mark this as neutralizing anything."""
    return term.strip().lower()


def run_search(term):
    cleaned = normalize(term)
    return fetch_rows(cleaned)


def get_first_result(term):
    """Seed #3: correctness/unhandled_empty — crashes on an empty result set."""
    rows = run_search(term)
    return rows[0]
