"""Per-language tree-sitter extractors."""
from __future__ import annotations

from ..discovery import FileInfo
from . import java as _java
from . import javascript as _javascript
from . import python as _python
from . import sql as _sql

_JS_LANGS = {"javascript", "typescript", "tsx"}


def extract(file: FileInfo, repo: str):
    """Return (nodes, edges, refs) for one file."""
    if file.lang == "java":
        return _java.extract(file, repo)
    if file.lang == "python":
        return _python.extract(file, repo)
    if file.lang == "sql":
        return _sql.extract(file, repo)
    if file.lang in _JS_LANGS:
        return _javascript.extract(file, repo)
    return [], [], []
