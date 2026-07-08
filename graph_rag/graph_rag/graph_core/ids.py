"""Stable node identity.

id = hash(repo + kind + fqn). Never line-based: editing a body changes the
body_hash but keeps the id, so incremental re-index patches in place.
"""
from __future__ import annotations

import hashlib


def make_id(repo: str, fqn: str, kind: str) -> str:
    raw = f"{repo}\x00{kind}\x00{fqn}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def body_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
