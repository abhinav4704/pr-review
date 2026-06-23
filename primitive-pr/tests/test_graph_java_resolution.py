from __future__ import annotations

from pathlib import Path

import pytest

from pr_review import graph as graph_mod
from pr_review.graph import build_graph


pytestmark = pytest.mark.skipif(
    "java" not in graph_mod._LANGS,
    reason="tree-sitter-java is not available in this environment",
)


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_java_explicit_import_resolves_correct_class_and_method(tmp_path: Path):
    _write(
        tmp_path / "com" / "acme" / "a" / "Service.java",
        "package com.acme.a;\n"
        "public class Service {\n"
        "  public int run() { return 1; }\n"
        "}\n",
    )
    _write(
        tmp_path / "com" / "acme" / "b" / "Service.java",
        "package com.acme.b;\n"
        "public class Service {\n"
        "  public int run() { return 2; }\n"
        "}\n",
    )
    _write(
        tmp_path / "com" / "acme" / "app" / "Use.java",
        "package com.acme.app;\n"
        "import com.acme.b.Service;\n"
        "public class Use {\n"
        "  public int x() {\n"
        "    Service s = new Service();\n"
        "    return s.run();\n"
        "  }\n"
        "}\n",
    )

    cg = build_graph(str(tmp_path))

    caller = "com/acme/app/Use.java::Use.x"
    target_ok = "com/acme/b/Service.java::Service.run"
    target_bad = "com/acme/a/Service.java::Service.run"

    edge_ok = cg.g.get_edge_data(caller, target_ok)
    assert edge_ok is not None
    assert edge_ok.get("type") == "calls"
    assert edge_ok.get("confidence") in {"unique", "same_file"}

    assert cg.g.get_edge_data(caller, target_bad) is None


def test_java_same_package_resolution_without_import(tmp_path: Path):
    _write(
        tmp_path / "pkg" / "a" / "Util.java",
        "package pkg.a;\n"
        "public class Util {\n"
        "  public int ping() { return 1; }\n"
        "}\n",
    )
    _write(
        tmp_path / "pkg" / "b" / "Util.java",
        "package pkg.b;\n"
        "public class Util {\n"
        "  public int ping() { return 2; }\n"
        "}\n",
    )
    _write(
        tmp_path / "pkg" / "b" / "Caller.java",
        "package pkg.b;\n"
        "public class Caller {\n"
        "  public int go() {\n"
        "    Util u = new Util();\n"
        "    return u.ping();\n"
        "  }\n"
        "}\n",
    )

    cg = build_graph(str(tmp_path))

    caller = "pkg/b/Caller.java::Caller.go"
    target_ok = "pkg/b/Util.java::Util.ping"
    target_bad = "pkg/a/Util.java::Util.ping"

    edge_ok = cg.g.get_edge_data(caller, target_ok)
    assert edge_ok is not None
    assert edge_ok.get("type") == "calls"
    assert edge_ok.get("confidence") in {"unique", "same_file"}

    assert cg.g.get_edge_data(caller, target_bad) is None


def test_java_single_constructor_infers_autowired_dependency(tmp_path: Path):
    _write(
        tmp_path / "com" / "acme" / "repo" / "UserRepo.java",
        "package com.acme.repo;\n"
        "public class UserRepo { }\n",
    )
    _write(
        tmp_path / "com" / "acme" / "svc" / "UserService.java",
        "package com.acme.svc;\n"
        "import com.acme.repo.UserRepo;\n"
        "public class UserService {\n"
        "  private final UserRepo repo;\n"
        "  public UserService(UserRepo repo) { this.repo = repo; }\n"
        "}\n",
    )

    cg = build_graph(str(tmp_path))

    edge = cg.g.get_edge_data(
        "com/acme/svc/UserService.java::UserService",
        "com/acme/repo/UserRepo.java::UserRepo",
    )
    assert edge is not None
    assert edge.get("type") == "autowired"
