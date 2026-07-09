"""Lazy tree-sitter parser construction (tree-sitter >= 0.22 API)."""
from __future__ import annotations

from tree_sitter import Language, Parser

_PARSERS: dict[str, Parser] = {}


def get_parser(lang: str) -> Parser:
    if lang not in _PARSERS:
        if lang == "java":
            import tree_sitter_java as ts
            language = ts.language()
        elif lang == "python":
            import tree_sitter_python as ts
            language = ts.language()
        elif lang == "javascript":
            import tree_sitter_javascript as ts
            language = ts.language()          # also parses JSX
        elif lang == "typescript":
            import tree_sitter_typescript as ts
            language = ts.language_typescript()
        elif lang == "tsx":
            import tree_sitter_typescript as ts
            language = ts.language_tsx()       # TSX grammar (JSX-aware)
        else:
            raise ValueError(f"no tree-sitter grammar for {lang!r}")
        _PARSERS[lang] = Parser(Language(language))
    return _PARSERS[lang]
