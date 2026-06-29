#!/usr/bin/env python3
"""Standalone SCIP-python smoke test — independent of the graph_rag code.

Checks, in order:
  1. Locate the scip-python binary (env var, local scip_tooling install, PATH;
     Windows .cmd/.ps1 aware).
  2. Run `scip-python --version`.
  3. Actually index a throwaway 2-file Python project in a temp dir and confirm
     a non-empty index.scip is produced.

Usage:
    python scip_check.py            # auto-locate the binary
    python scip_check.py <path>     # use this binary explicitly
    SCIP_PYTHON_BIN=... python scip_check.py

Exit code 0 = scip is installed AND can produce an index. Non-zero = problem
(the message says which step failed). No network access is required to *run*
scip-python, so this works behind a proxy/Zscaler once it's installed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile


def find_binary(explicit: str | None) -> str | None:
    """Same lookup order the pipeline uses, but Windows-extension aware."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("SCIP_PYTHON_BIN")
    if env:
        candidates.append(env)

    here = os.path.dirname(os.path.abspath(__file__))
    bindir = os.path.join(here, "scip_tooling", "node_modules", ".bin")
    # On Windows npm writes scip-python.cmd / .ps1, not a bare file.
    for name in ("scip-python", "scip-python.cmd", "scip-python.ps1", "scip-python.exe"):
        candidates.append(os.path.join(bindir, name))

    for c in candidates:
        if c and os.path.exists(c):
            return c
    return shutil.which("scip-python")


def run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    # shell=True on Windows so .cmd shims are invoked correctly.
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=180,
        shell=(os.name == "nt"),
    )


def main() -> int:
    explicit = sys.argv[1] if len(sys.argv) > 1 else None

    print("1) locating scip-python ...")
    binary = find_binary(explicit)
    if not binary:
        print("   FAIL: scip-python not found.")
        print("   - install: npm install --prefix scip_tooling @sourcegraph/scip-python@0.6.6")
        print("   - or set SCIP_PYTHON_BIN to its full path (on Windows include .cmd)")
        return 2
    print(f"   found: {binary}")

    print("2) checking version ...")
    try:
        r = run([binary, "--version"])
    except Exception as e:
        print(f"   FAIL: could not execute it: {type(e).__name__}: {e}")
        return 3
    if r.returncode != 0:
        print(f"   FAIL: exit {r.returncode}")
        print("   stderr:", (r.stderr or "").strip()[:500])
        return 3
    print(f"   version: {(r.stdout or r.stderr).strip()}")

    print("3) indexing a throwaway project ...")
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "a.py"), "w") as f:
            f.write("def greet(name):\n    return hello(name)\n")
        with open(os.path.join(d, "hello.py"), "w") as f:
            f.write("def hello(name):\n    return 'hi ' + name\n")
        try:
            # cwd MUST be the repo root — scip-python discovers files relative to it.
            # --project-name/-version are required when the dir isn't a git repo
            # (otherwise scip-python crashes deriving the version from `git rev-parse`).
            r = run([
                binary, "index",
                "--project-name", "scip-smoke-test",
                "--project-version", "0.0.0",
                "--output", "index.scip",
            ], cwd=d)
        except Exception as e:
            print(f"   FAIL: index crashed: {type(e).__name__}: {e}")
            return 4
        out = os.path.join(d, "index.scip")
        size = os.path.getsize(out) if os.path.exists(out) else 0
        if r.returncode != 0 or size == 0:
            print(f"   FAIL: exit {r.returncode}, index.scip size {size} bytes")
            print("   stdout:", (r.stdout or "").strip()[:500])
            print("   stderr:", (r.stderr or "").strip()[:500])
            return 4
        print(f"   OK: produced index.scip ({size} bytes)")

    print("\nSCIP is installed and working. ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
