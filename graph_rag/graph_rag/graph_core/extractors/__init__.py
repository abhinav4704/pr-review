"""Per-language tree-sitter extractors."""
from __future__ import annotations

from ..discovery import FileInfo
from . import java as _java
from . import python as _python


def extract(file: FileInfo, repo: str):
    """Return (nodes, edges, refs) for one file."""
    if file.lang == "java":
        return _java.extract(file, repo)
    if file.lang == "python":
        return _python.extract(file, repo)
    return [], [], []
