from __future__ import annotations

from pathlib import Path

from analyser.audit import run_audit


def test_run_audit_smoke(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    # Two files where b.py depends on helper() in a.py.
    (repo / "a.py").write_text(
        "def helper(x: int) -> int:\n"
        "    return x + 1\n"
        "\n"
        "def dead_func() -> int:\n"
        "    return 42\n",
        encoding="utf-8",
    )
    (repo / "b.py").write_text(
        "from a import helper\n"
        "\n"
        "def use_helper(v: int) -> int:\n"
        "    return helper(v)\n",
        encoding="utf-8",
    )

    result = run_audit(str(repo), depth=2, top_n=10)

    assert result.summary.files == 2
    assert result.summary.definitions >= 3
    assert "blast_radius" in result.breakage
    assert "orphans" in result.deadcode
    assert "hotspots" in result.architecture


def test_detects_import_cycle(tmp_path: Path) -> None:
    repo = tmp_path / "repo_cycle"
    repo.mkdir()

    (repo / "a.py").write_text(
        "from b import b_func\n"
        "\n"
        "def a_func() -> int:\n"
        "    return b_func()\n",
        encoding="utf-8",
    )
    (repo / "b.py").write_text(
        "from a import a_func\n"
        "\n"
        "def b_func() -> int:\n"
        "    return 1\n",
        encoding="utf-8",
    )

    result = run_audit(str(repo), depth=2, top_n=10)
    assert len(result.architecture["import_cycles"]) >= 1


def test_excludes_generated_and_tests_from_deadcode(tmp_path: Path) -> None:
    repo = tmp_path / "repo_filters"
    (repo / "generated").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)

    (repo / "generated" / "gen.py").write_text(
        "def generated_helper() -> int:\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_sample.py").write_text(
        "def test_unused() -> None:\n"
        "    assert True\n",
        encoding="utf-8",
    )
    (repo / "app.py").write_text(
        "def live() -> int:\n"
        "    return 2\n",
        encoding="utf-8",
    )

    result = run_audit(str(repo), depth=2, top_n=10)
    orphan_paths = {item["path"] for item in result.deadcode["orphans"]}

    assert "generated/gen.py" not in orphan_paths
    assert "tests/test_sample.py" not in orphan_paths


def test_detects_missing_symbol_import(tmp_path: Path) -> None:
    repo = tmp_path / "repo_missing_symbol"
    repo.mkdir()

    (repo / "a.py").write_text(
        "def available() -> int:\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (repo / "b.py").write_text(
        "from a import removed_symbol\n"
        "\n"
        "def call() -> int:\n"
        "    return removed_symbol()\n",
        encoding="utf-8",
    )

    result = run_audit(str(repo), depth=2, top_n=10)
    missing = result.breakage.get("missing_symbols", [])

    assert len(missing) >= 1
    assert any(item["symbol"] == "removed_symbol" for item in missing)


def test_scans_secrets_and_dependencies(tmp_path: Path) -> None:
    repo = tmp_path / "repo_scanners"
    repo.mkdir()

    (repo / "requirements.txt").write_text(
        "requests==2.32.0\n"
        "numpy==2.0.0\n",
        encoding="utf-8",
    )
    (repo / "app.py").write_text(
        "import requests\n"
        "import pandas\n"
        "\n"
        "API_KEY = \"AKIAABCDEFGHIJKLMNOP\"\n"
        "\n"
        "def run() -> int:\n"
        "    return 1\n",
        encoding="utf-8",
    )

    result = run_audit(str(repo), depth=2, top_n=10, include_osv=False)

    secrets = result.security.get("findings", [])
    deps = result.dependencies.get("findings", [])

    assert any(item["type"] == "pattern" for item in secrets)
    assert any(item.get("type") == "undeclared" and item.get("package") == "pandas" for item in deps)
    assert any(item.get("type") == "unused" and item.get("package") == "numpy" for item in deps)
    assert result.dependencies.get("osv_status") == "disabled"
