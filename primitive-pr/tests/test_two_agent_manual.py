from __future__ import annotations

from pathlib import Path

from pr_review.findings import Finding
from pr_review.two_agent_review import run_two_agent_review_manual
import pr_review.two_agent_review as ta


def _finding(file_path: str, line: int, title: str, severity: str = "high") -> Finding:
    return Finding(
        category="bug",
        severity=severity,
        file=file_path,
        line=line,
        title=title,
        explanation="e",
        evidence="code",
        recommendation="fix",
    )


def test_manual_orchestrator_skips_diff_parse(monkeypatch, tmp_path: Path):
    (tmp_path / "a.py").write_text(
        "def foo():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "def bar():\n"
        "    return 2\n",
        encoding="utf-8",
    )

    def _boom_parse(_text):
        raise AssertionError("parse_diff should not be called in manual mode")

    def _fake_pass_whole_file(path, src_path, complete, budget, diff_text=""):
        return [_finding(path, 1, f"whole-file-{path}")]

    def _fake_run_dependency_prompt(complete, prompt_id, prompt, diff_block, budget):
        origin = prompt_id.split("::", 1)[0]
        return prompt_id, origin, [_finding("b.py", 1, "dep-break", severity="critical")]

    monkeypatch.setattr(ta, "parse_diff", _boom_parse)
    monkeypatch.setattr(ta, "pass_whole_file", _fake_pass_whole_file)
    monkeypatch.setattr(ta, "_run_dependency_prompt", _fake_run_dependency_prompt)

    prompt_id = "a.py::foo"
    prompt_text = (
        "### Changed function: foo (a.py, lines 1-2)\n"
        "```\n1 def foo():\n2     return 1\n```\n\n"
        "### Dependent function: bar (b.py, lines 1-2)\n"
        "```\n1 def bar():\n2     return 2\n```\n"
    )
    prompts = {prompt_id: prompt_text}
    chains = {
        prompt_id: {
            "changed_file": "a.py",
            "changed_function": "foo",
            "dependent_files": ["b.py"],
            "chain_paths": ["a.py -> b.py"],
        }
    }

    out = run_two_agent_review_manual(
        cg=None,
        src_path=str(tmp_path),
        selected_files=["a.py"],
        prompts=prompts,
        chains=chains,
        complete=lambda _s, _u: "[]",
        max_workers=2,
    )

    assert out
    by_path = {fr.path: fr for fr in out}
    assert "a.py" in by_path
    assert by_path["a.py"].file_findings
    assert by_path["a.py"].dependency_findings
    assert "foo" in by_path["a.py"].dependency_findings_by_function


def test_manual_dependency_filters_placeholder_paths(monkeypatch, tmp_path: Path):
    (tmp_path / "a.py").write_text(
        "def foo():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text(
        "def bar():\n"
        "    return 2\n",
        encoding="utf-8",
    )

    def _fake_pass_whole_file(path, src_path, complete, budget, diff_text=""):
        return [_finding(path, 1, f"whole-file-{path}")]

    def _fake_run_dependency_prompt(complete, prompt_id, prompt, diff_block, budget):
        # This intentionally returns a placeholder path to ensure enrichment/filtering
        # keeps only graph-grounded dependency output.
        return prompt_id, "a.py", [
            _finding("path/to/dependent_file.py", 99, "placeholder-dep", severity="critical")
        ]

    monkeypatch.setattr(ta, "pass_whole_file", _fake_pass_whole_file)
    monkeypatch.setattr(ta, "_run_dependency_prompt", _fake_run_dependency_prompt)

    prompt_id = "a.py::foo"
    prompt_text = (
        "### Changed function: foo (a.py, lines 1-2)\n"
        "```\n1 def foo():\n2     return 1\n```\n\n"
        "### Dependent function: bar (b.py, lines 1-2)\n"
        "```\n1 def bar():\n2     return 2\n```\n"
    )
    prompts = {prompt_id: prompt_text}
    chains = {
        prompt_id: {
            "changed_file": "a.py",
            "changed_function": "foo",
            "dependent_files": ["b.py"],
            "chain_paths": ["a.py -> b.py"],
        }
    }

    out = run_two_agent_review_manual(
        cg=None,
        src_path=str(tmp_path),
        selected_files=["a.py"],
        prompts=prompts,
        chains=chains,
        complete=lambda _s, _u: "[]",
        max_workers=2,
    )

    dep_findings = out[0].dependency_findings
    assert dep_findings
    assert all("path/to/" not in f.file for f in dep_findings)
    assert all(f.provenance_status != "unverified_llm_claim" for f in dep_findings)
    assert any(f.file == "b.py" for f in dep_findings)
