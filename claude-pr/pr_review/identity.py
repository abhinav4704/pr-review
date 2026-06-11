"""Compact "identity cards" for changed functions.

When the breaking-change pass reviews the *other* files that call a changed
function, sending the full source of that function (and every caller) blows up
the token budget. Instead we send a small identity card:

    qualname + signature (old -> new) + return hint + docstring + optional summary

Strategy is hybrid (chosen with the user):
  * structural card  — free, deterministic; extracted from the graph/source.
  * LLM summary      — added only when the function source exceeds a size
                       threshold, and only if a Nova client is supplied. Cached
                       by node id so each large function is summarised once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional

from .graph import CodeGraph

# A function over this many source lines gets an LLM summary (if nova provided).
LLM_SUMMARY_THRESHOLD = 40

_SUMMARY_SYSTEM = (
    "You summarise a single function for a reviewer who must decide whether "
    "callers in OTHER files still work after it changed. Be terse and factual."
)
_SUMMARY_INSTRUCTIONS = (
    "Summarise this function in <=3 short lines:\n"
    "inputs: <params and their meaning>\n"
    "outputs: <return type/shape and error behavior>\n"
    "behavior: <one sentence on what it does that a caller relies on>\n\n"
    "FUNCTION:\n{source}"
)


@dataclass
class IdentityCard:
    node_id: str
    qualname: str
    kind: str
    path: str
    signature: str          # current (new) signature line(s)
    returns: str            # heuristic return hint ("" if unknown)
    doc: str                # leading docstring, trimmed ("" if none)
    summary: str = ""       # LLM summary for large functions ("" otherwise)
    change_type: str = "behavior"


# ── signature / docstring extraction ──────────────────────────────────────────

def _signature_from_source(src: str, kind: str) -> str:
    """Best-effort extraction of the declaration line(s) from source.

    Handles Python `def ...:` (possibly multi-line) and brace-style
    declarations for JS/TS/Java/Go by reading up to the first `{` or `:`.
    """
    if not src.strip():
        return ""
    text = src.lstrip("\n")
    # Python-style: accumulate lines until one ends in ':' (signature terminator)
    if re.match(r"\s*(async\s+def|def|class)\b", text):
        out = []
        for line in text.splitlines():
            stripped = line.rstrip()
            out.append(stripped)
            if stripped.endswith(":"):
                break
        return "\n".join(out).strip()
    # brace-style: read until first '{'
    head = text.split("{", 1)[0].strip()
    return head or text.splitlines()[0].strip()


_RETURN_HINTS = (
    re.compile(r"->\s*([^:#\n]+):"),       # python annotation
    re.compile(r"\breturns?\s+([A-Za-z_][\w\.\[\]]*)", re.IGNORECASE),
)


def _return_hint(signature: str, doc: str) -> str:
    for rx in _RETURN_HINTS:
        m = rx.search(signature) or rx.search(doc)
        if m:
            return m.group(1).strip()
    return ""


def _docstring(src: str) -> str:
    m = re.search(r'"""(.*?)"""', src, re.DOTALL) or re.search(r"'''(.*?)'''", src, re.DOTALL)
    if m:
        return " ".join(m.group(1).split())[:240]
    return ""


# ── card builder ──────────────────────────────────────────────────────────────

def build_identity_card(
    cg: CodeGraph,
    node_id: str,
    change_type: str = "behavior",
    nova=None,
    llm_threshold: int = LLM_SUMMARY_THRESHOLD,
    cache: Optional[Dict[str, IdentityCard]] = None,
) -> Optional[IdentityCard]:
    if cache is not None and node_id in cache:
        return cache[node_id]
    if not cg.has(node_id):
        return None

    d = cg.node(node_id)
    src = cg.source(node_id)
    signature = _signature_from_source(src, d.get("kind", ""))
    doc = _docstring(src)
    card = IdentityCard(
        node_id=node_id,
        qualname=d.get("qualname") or d.get("name", ""),
        kind=d.get("kind", ""),
        path=d.get("path", ""),
        signature=signature,
        returns=_return_hint(signature, doc),
        doc=doc,
        change_type=change_type,
    )

    n_lines = src.count("\n") + 1 if src else 0
    if nova is not None and n_lines > llm_threshold:
        try:
            card.summary = nova.complete(
                _SUMMARY_SYSTEM,
                _SUMMARY_INSTRUCTIONS.format(source=src[:6000]),
            ).strip()
        except Exception:
            card.summary = ""

    if cache is not None:
        cache[node_id] = card
    return card


def render_card(card: IdentityCard, old_signature: Optional[str] = None) -> str:
    """Compact markdown rendering for inclusion in a prompt."""
    lines = [f"CHANGED {card.kind.upper() or 'FUNCTION'}: {card.qualname}  ({card.path})",
             f"change type: {card.change_type}"]
    if old_signature and old_signature.strip() and old_signature.strip() != card.signature.strip():
        lines.append(f"old signature: {old_signature.strip()}")
        lines.append(f"new signature: {card.signature}")
    else:
        lines.append(f"signature: {card.signature}")
    if card.returns:
        lines.append(f"returns: {card.returns}")
    if card.doc:
        lines.append(f"doc: {card.doc}")
    if card.summary:
        lines.append(f"summary:\n{card.summary}")
    return "\n".join(lines)
