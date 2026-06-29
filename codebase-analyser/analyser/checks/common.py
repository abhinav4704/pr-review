"""Shared filtering helpers for deterministic checks."""

from __future__ import annotations


EXCLUDED_PATH_PARTS = {
    "tests",
    "test",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "vendor",
    "generated",
    ".venv",
    "venv",
}


def should_exclude_path(path: str) -> bool:
    """True when a source path should be excluded from core deterministic checks."""
    normalized = (path or "").replace("\\", "/").strip("/").lower()
    if not normalized:
        return False
    parts = [part for part in normalized.split("/") if part]
    return any(part in EXCLUDED_PATH_PARTS for part in parts)
