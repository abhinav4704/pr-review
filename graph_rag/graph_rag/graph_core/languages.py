"""Lazy tree-sitter parser construction (tree-sitter >= 0.22 API)."""
from __future__ import annotations

from tree_sitter import Language, Parser

_PARSERS: dict[str, Parser] = {}


def get_parser(lang: str) -> Parser:
    if lang not in _PARSERS:
        if lang == "java":
            import tree_sitter_java as ts
        elif lang == "python":
            import tree_sitter_python as ts
        else:
            raise ValueError(f"no tree-sitter grammar for {lang!r}")
        _PARSERS[lang] = Parser(Language(ts.language()))
    return _PARSERS[lang]
