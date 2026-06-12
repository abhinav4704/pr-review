"""Decide which changed files are worth sending to the reviewer.

Whole-file review feeds every changed file to the LLM, so we must skip files that
are generated, vendored, binary, or pure dependency manifests — reviewing them
wastes tokens and produces noise. Lock/generated/binary files are ALWAYS skipped;
dependency manifests (requirements.txt, package.json, …) are skipped by default
but can be opted back in.
"""

from __future__ import annotations

import os

# Whole directories that are generated / vendored / build output.
IGNORE_DIRS = {
    "node_modules", "dist", "build", "out", "target", "vendor", "coverage",
    ".venv", "venv", "env", "__pycache__", ".git", ".next", ".nuxt", ".cache",
    ".idea", ".tox", "site-packages",
}

# Exact lock / generated filenames (compared lowercase).
IGNORE_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "poetry.lock", "pipfile.lock", "cargo.lock", "composer.lock",
    "gemfile.lock", "go.sum",
}

# Binary / minified / map / asset suffixes (never useful to review as text).
IGNORE_SUFFIXES = (
    ".min.js", ".min.css", ".map", ".lock", ".snap",
    # images / media
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp",
    ".mp4", ".mp3", ".mov", ".avi", ".wav", ".pdf",
    # fonts
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    # archives / binaries / data
    ".zip", ".tar", ".gz", ".tgz", ".rar", ".7z",
    ".wasm", ".so", ".dll", ".dylib", ".class", ".o", ".a", ".bin",
    ".pkl", ".pyc", ".parquet", ".csv", ".tsv",
)

# Dependency manifests / config — skipped by default, opt-in via include_config.
# setup.py is intentionally excluded: it can contain arbitrary build/install code
# that is worth reviewing (e.g. injected commands, secret exfil, malicious hooks).
MANIFEST_FILENAMES = {
    "package.json", "pipfile", "go.mod", "setup.cfg",
    "pyproject.toml", "cargo.toml", "composer.json", "gemfile",
    "dockerfile", ".gitignore", ".dockerignore",
}


def _is_manifest(base: str) -> bool:
    if base in MANIFEST_FILENAMES:
        return True
    # requirements.txt, requirements-dev.txt, dev-requirements.txt, ...
    return base.endswith(".txt") and "requirements" in base


def should_review(path: str, include_config: bool = False) -> bool:
    """True if the changed file is worth sending to the reviewer."""
    p = path.replace("\\", "/")
    base = os.path.basename(p).lower()
    dirs = p.lower().split("/")[:-1]

    if any(d in IGNORE_DIRS for d in dirs):
        return False
    if base in IGNORE_FILENAMES:
        return False
    if any(base.endswith(suf) for suf in IGNORE_SUFFIXES):
        return False
    if not include_config and _is_manifest(base):
        return False
    return True
