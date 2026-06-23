from __future__ import annotations

from pathlib import Path

import pytest

from pr_review import graph as graph_mod
from pr_review.graph import GraphFeatureFlags, build_graph


def test_graph_feature_flags_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PR_REVIEW_ENABLE_LANGUAGE_EXTRACTORS", "true")
    monkeypatch.setenv("PR_REVIEW_ENABLE_CROSS_LANGUAGE_API_LINKS", "1")
    monkeypatch.setenv("PR_REVIEW_STRICT_LANGUAGE_MODE", "yes")
    monkeypatch.setenv("PR_REVIEW_MAX_REEXPORT_DEPTH", "5")

    flags = GraphFeatureFlags.from_env()

    assert flags.enable_language_specific_extractors is True
    assert flags.enable_cross_language_api_links is True
    assert flags.strict_language_mode is True
    assert flags.max_reexport_depth == 5


@pytest.mark.skipif(
    "javascript" not in graph_mod._LANGS,
    reason="tree-sitter-javascript is not available in this environment",
)
def test_language_extractor_falls_back_when_not_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    js_file = tmp_path / "index.js"
    js_file.write_text(
        "function foo() { return 1; }\n"
        "function bar() { return foo(); }\n",
        encoding="utf-8",
    )

    def _boom(self, _root):
        raise RuntimeError("forced js extractor failure")

    monkeypatch.setattr(graph_mod._JsTsExtractor, "extract", _boom)

    flags = GraphFeatureFlags(enable_language_specific_extractors=True)
    cg = build_graph(str(tmp_path), feature_flags=flags)

    # Build should remain successful via fallback extractor path.
    assert "index.js" in cg.g


@pytest.mark.skipif(
    "javascript" not in graph_mod._LANGS,
    reason="tree-sitter-javascript is not available in this environment",
)
def test_language_extractor_raises_in_strict_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    js_file = tmp_path / "index.js"
    js_file.write_text("function foo() { return 1; }\n", encoding="utf-8")

    def _boom(self, _root):
        raise RuntimeError("forced js extractor failure")

    monkeypatch.setattr(graph_mod._JsTsExtractor, "extract", _boom)

    flags = GraphFeatureFlags(
        enable_language_specific_extractors=True,
        strict_language_mode=True,
    )

    with pytest.raises(RuntimeError, match="forced js extractor failure"):
        build_graph(str(tmp_path), feature_flags=flags)


@pytest.mark.skipif(
    "javascript" not in graph_mod._LANGS,
    reason="tree-sitter-javascript is not available in this environment",
)
def test_cross_language_api_linker_flag_controls_links(tmp_path: Path):
    (tmp_path / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users():\n"
        "    return []\n",
        encoding="utf-8",
    )
    (tmp_path / "client.js").write_text(
        "function callApi() { return fetch('/users'); }\n",
        encoding="utf-8",
    )

    off_cg = build_graph(
        str(tmp_path),
        feature_flags=GraphFeatureFlags(enable_cross_language_api_links=False),
    )
    on_cg = build_graph(
        str(tmp_path),
        feature_flags=GraphFeatureFlags(enable_cross_language_api_links=True),
    )

    caller = "client.js::callApi"
    target = "api.py::list_users"

    assert off_cg.g.get_edge_data(caller, target) is None

    edge = on_cg.g.get_edge_data(caller, target)
    assert edge is not None
    assert edge.get("type") == "uses"
    assert edge.get("inferred") is True
    assert edge.get("api_method") == "GET"
    assert edge.get("api_path") == "/users"
