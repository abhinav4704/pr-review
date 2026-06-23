from __future__ import annotations

from pathlib import Path

import pytest

from pr_review import graph as graph_mod
from pr_review.graph import build_graph


pytestmark = pytest.mark.skipif(
    "javascript" not in graph_mod._LANGS,
    reason="tree-sitter-javascript is not available in this environment",
)


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_ts_alias_wildcard_path_resolution(tmp_path: Path):
    _write(
        tmp_path / "tsconfig.json",
        '{\n'
        '  "compilerOptions": {\n'
        '    "baseUrl": ".",\n'
        '    "paths": {\n'
        '      "@core/*": ["src/core/*"]\n'
        '    }\n'
        '  }\n'
        '}\n',
    )
    _write(tmp_path / "src" / "core" / "ping.ts", "export function ping() { return 1; }\n")
    _write(
        tmp_path / "app.ts",
        "import { ping } from '@core/ping';\n"
        "function run() { return ping(); }\n",
    )

    cg = build_graph(str(tmp_path))

    edge = cg.g.get_edge_data("app.ts::run", "src/core/ping.ts::ping")
    assert edge is not None
    assert edge.get("type") == "calls"
    assert edge.get("confidence") in {"unique", "same_file"}


def test_ts_alias_exact_path_resolution(tmp_path: Path):
    _write(
        tmp_path / "tsconfig.json",
        '{\n'
        '  "compilerOptions": {\n'
        '    "baseUrl": ".",\n'
        '    "paths": {\n'
        '      "@utils": ["src/utils/index"]\n'
        '    }\n'
        '  }\n'
        '}\n',
    )
    _write(tmp_path / "src" / "utils" / "index.ts", "export function util() { return 1; }\n")
    _write(
        tmp_path / "app.ts",
        "import { util } from '@utils';\n"
        "function run() { return util(); }\n",
    )

    cg = build_graph(str(tmp_path))

    edge = cg.g.get_edge_data("app.ts::run", "src/utils/index.ts::util")
    assert edge is not None
    assert edge.get("type") == "calls"
    assert edge.get("confidence") in {"unique", "same_file"}
