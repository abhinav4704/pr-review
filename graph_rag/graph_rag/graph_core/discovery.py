"""Stage 0 — walk a repo, detect language, read + hash source files."""
from __future__ import annotations

import os
from dataclasses import dataclass

from .ids import body_hash

IGNORE_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    "build", "target", "dist", "out", "bin", ".idea", ".gradle", ".mvn",
    ".pytest_cache", ".mypy_cache", "site-packages",
}

EXT_LANG = {".java": "java", ".py": "python"}


@dataclass
class FileInfo:
    relpath: str
    abspath: str
    lang: str
    sha: str
    source: bytes


def discover(root: str) -> list[FileInfo]:
    root = os.path.abspath(root)
    out: list[FileInfo] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1]
            lang = EXT_LANG.get(ext)
            if not lang:
                continue
            abspath = os.path.join(dirpath, fn)
            try:
                with open(abspath, "rb") as fh:
                    src = fh.read()
            except OSError:
                continue
            rel = os.path.relpath(abspath, root)
            out.append(
                FileInfo(
                    relpath=rel,
                    abspath=abspath,
                    lang=lang,
                    sha=body_hash(src.decode("utf-8", "replace")),
                    source=src,
                )
            )
    return out
