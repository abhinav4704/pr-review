"""Import helpers for reusing primitive-pr in read-only mode."""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_primitive_on_path() -> Path:
    """Ensure sibling primitive-pr directory is importable as pr_review."""
    analyser_root = Path(__file__).resolve().parents[1]
    primitive_root = analyser_root.parent / "primitive-pr"
    if not primitive_root.exists():
        raise FileNotFoundError(
            f"primitive-pr folder not found at expected location: {primitive_root}"
        )
    primitive_root_str = str(primitive_root)
    if primitive_root_str not in sys.path:
        sys.path.insert(0, primitive_root_str)
    return primitive_root
