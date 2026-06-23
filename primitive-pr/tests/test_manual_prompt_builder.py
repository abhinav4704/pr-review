from __future__ import annotations

from pathlib import Path

from pr_review.graph import CodeGraph
from pr_review.prompt_builder import (
    build_prompts_for_selected_files,
    build_selected_file_chains,
)


def _add_fn(cg: CodeGraph, path: str, name: str, start: int, end: int) -> str:
    nid = f"{path}::{name}"
    cg.g.add_node(
        nid,
        kind="function",
        path=path,
        name=name,
        qualname=name,
        start_line=start,
        end_line=end,
        lang="python",
    )
    return nid


def test_manual_selected_files_prompt_and_chains(tmp_path: Path):
    src_root = tmp_path
    (src_root / "a.py").write_text(
        "def foo():\n"
        "    x = 1\n"
        "    return x\n",
        encoding="utf-8",
    )
    (src_root / "b.py").write_text(
        "from a import foo\n"
        "def bar():\n"
        "    return foo()\n",
        encoding="utf-8",
    )

    cg = CodeGraph(root=str(src_root))
    foo = _add_fn(cg, "a.py", "foo", 1, 3)
    bar = _add_fn(cg, "b.py", "bar", 2, 3)
    cg.g.add_edge(bar, foo, type="calls", confidence="unique")

    prompts = build_prompts_for_selected_files(cg, str(src_root), ["a.py"], depth=2)
    assert prompts
    assert "a.py::foo" in prompts
    assert "Dependent" in prompts["a.py::foo"]

    chains = build_selected_file_chains(cg, ["a.py"], depth=2)
    assert "a.py::foo" in chains
    entry = chains["a.py::foo"]
    assert entry["changed_file"] == "a.py"
    assert entry["changed_function"] == "foo"
    assert "b.py" in entry["dependent_files"]
