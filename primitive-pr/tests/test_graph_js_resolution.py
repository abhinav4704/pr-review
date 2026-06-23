from __future__ import annotations

from pathlib import Path

import pytest

from pr_review import graph as graph_mod
from pr_review.graph import GraphFeatureFlags, build_graph


pytestmark = pytest.mark.skipif(
	"javascript" not in graph_mod._LANGS,
	reason="tree-sitter-javascript is not available in this environment",
)


def _write(p: Path, content: str) -> None:
	p.parent.mkdir(parents=True, exist_ok=True)
	p.write_text(content, encoding="utf-8")


def test_js_namespace_import_disambiguates_member_call(tmp_path: Path):
	_write(
		tmp_path / "lib.js",
		"export function foo() { return 1; }\n",
	)
	_write(
		tmp_path / "use.js",
		"import * as lib from './lib';\n"
		"function go() { return lib.foo(); }\n",
	)

	cg = build_graph(str(tmp_path))

	caller = "use.js::go"
	target = "lib.js::foo"
	edge = cg.g.get_edge_data(caller, target)

	assert edge is not None
	assert edge.get("type") == "calls"
	assert edge.get("confidence") in {"unique", "same_file"}


def test_js_default_import_uses_local_binding_target(tmp_path: Path):
	_write(
		tmp_path / "lib.js",
		"export default function dep() { return 1; }\n",
	)
	_write(
		tmp_path / "use.js",
		"import dep from './lib';\n"
		"function go() { return dep(); }\n",
	)

	cg = build_graph(str(tmp_path))

	caller = "use.js::go"
	target = "lib.js::dep"
	edge = cg.g.get_edge_data(caller, target)

	assert edge is not None
	assert edge.get("type") == "calls"
	assert edge.get("confidence") in {"unique", "same_file"}


def test_js_reexport_depth_allows_resolution_when_within_limit(tmp_path: Path):
	_write(tmp_path / "base.js", "export function core() { return 1; }\n")
	_write(tmp_path / "mid.js", "export { core } from './base';\n")
	_write(tmp_path / "barrel.js", "export { core } from './mid';\n")
	_write(
		tmp_path / "app.js",
		"import { core } from './barrel';\n"
		"function run() { return core(); }\n",
	)

	shallow = build_graph(
		str(tmp_path),
		feature_flags=GraphFeatureFlags(max_reexport_depth=1),
	)
	deep = build_graph(
		str(tmp_path),
		feature_flags=GraphFeatureFlags(max_reexport_depth=2),
	)

	caller = "app.js::run"
	target = "base.js::core"

	assert shallow.g.get_edge_data(caller, target) is None

	edge = deep.g.get_edge_data(caller, target)
	assert edge is not None
	assert edge.get("type") == "calls"
	assert edge.get("confidence") in {"unique", "same_file"}


def test_js_module_bound_unresolved_does_not_fanout_globally(tmp_path: Path):
	_write(tmp_path / "lib.js", "export function real() { return 1; }\n")
	_write(tmp_path / "other.js", "export function missing() { return 2; }\n")
	_write(
		tmp_path / "app.js",
		"import * as lib from './lib';\n"
		"function run() { return lib.missing(); }\n",
	)

	cg = build_graph(str(tmp_path))

	caller = "app.js::run"
	wrong_target = "other.js::missing"
	assert cg.g.get_edge_data(caller, wrong_target) is None
