"""Basic secrets scanner with regex and entropy heuristics."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, List

from analyser.checks.common import should_exclude_path

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)-----BEGIN (RSA|EC|OPENSSH|DSA) PRIVATE KEY-----"),
    re.compile(r"(?i)ghp_[A-Za-z0-9]{36}"),
]

_ALLOWED_NAME_HINTS = {"example", "sample", "dummy", "test", "placeholder"}
_SCAN_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yml", ".yaml", ".env", ".ini", ".toml", ".md"}


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    freq = {ch: value.count(ch) / len(value) for ch in set(value)}
    return -sum(p * math.log2(p) for p in freq.values())


def _is_likely_false_positive(line: str) -> bool:
    lowered = line.lower()
    return any(hint in lowered for hint in _ALLOWED_NAME_HINTS)


def scan_secrets(repo_root: str, entropy_threshold: float = 3.8) -> Dict[str, List[dict]]:
    """Scan repository text files for likely leaked secrets."""
    root = Path(repo_root)
    findings: List[dict] = []

    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue

        rel = file_path.relative_to(root).as_posix()
        if should_exclude_path(rel):
            continue

        suffix = file_path.suffix.lower()
        if suffix not in _SCAN_SUFFIXES and file_path.name != ".env":
            continue

        try:
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for idx, line in enumerate(lines, start=1):
            if _is_likely_false_positive(line):
                continue

            for pattern in _SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        {
                            "severity": "high",
                            "path": rel,
                            "line": idx,
                            "type": "pattern",
                            "message": "Potential hardcoded secret detected by pattern.",
                            "snippet": line.strip()[:240],
                        }
                    )
                    break

            # entropy heuristic on long tokens
            for token in re.findall(r"[A-Za-z0-9_\-/+=]{20,}", line):
                entropy = _shannon_entropy(token)
                if entropy >= entropy_threshold:
                    findings.append(
                        {
                            "severity": "medium",
                            "path": rel,
                            "line": idx,
                            "type": "entropy",
                            "message": f"High-entropy token detected (entropy={entropy:.2f}).",
                            "snippet": token[:120],
                        }
                    )

    findings.sort(key=lambda item: (item["path"], item["line"], item["type"]))
    return {"findings": findings}
