"""Review findings: data model, JSON parsing, severity helpers, dedupe.

Self-contained — no dependency on any other review code.

Severity meaning (project convention):
    critical  -> something breaks (caller errors / crash)
    high      -> security vulnerability or exposed secret/key
    medium    -> performance / unoptimized
    low       -> nice-to-have improvement
    info      -> purely informational
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import List, Optional

SEVERITY_WEIGHT = {"critical": 10, "high": 6, "medium": 3, "low": 1, "info": 0}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
SEV_BADGE = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    "info": "⚪",
}

# The output contract we ask every pass to follow.
FINDINGS_SCHEMA = """This is a PULL REQUEST review. You are shown the CHANGED lines (the
diff / the lines marked >>>) and the surrounding code/full file as CONTEXT. Only report
problems that the change INTRODUCES or that the change BREAKS. Use the full code as context
to judge (e.g. to spot vulnerabilities), but do NOT report pre-existing issues that are
unrelated to this change. If the change is fine, return [].

Return ONLY a JSON array (no prose, no markdown fences). Each element:
{
  "kind": "issue" | "suggestion",
  "category": "breaking" | "bug" | "vulnerability" | "optimization" | "suggestion",
  "severity": "critical" | "high" | "medium" | "low" | "info",
  "file": string,            // file path
  "line": integer,           // most relevant line number (0 if unknown)
  "title": string,           // one short sentence
  "explanation": string,     // 2-4 sentences: what changed and why it's a problem
  "evidence": string,        // short code snippet showing the problem
  "recommendation": string   // concrete actionable fix
}

Categories & severity:
- breaking   -> the change breaks callers or crashes at runtime. severity: critical.
- bug        -> the changed logic is wrong / misuses something. severity: critical or high
                by impact.
- vulnerability -> something EXPOSED by the change (secret/API key/credential/token) is the
                worst: severity critical. A security weakness the change leaves unaddressed
                (SQL injection, unsanitized input, missing auth/validation) is severity high,
                or medium if lower-risk.
- optimization -> the change is unoptimized / has a performance problem. severity: medium.
- suggestion  -> optional improvement. kind "suggestion", severity low.
If you find nothing, return []."""


@dataclass
class Finding:
    category: str
    severity: str
    file: str
    line: int
    title: str
    explanation: str
    evidence: str
    recommendation: str
    kind: str = "issue"

    @classmethod
    def from_dict(cls, d: dict, default_file: str = "") -> Optional["Finding"]:
        try:
            severity = str(d.get("severity", "low")).lower()
            if severity not in SEVERITY_WEIGHT:
                severity = "low"
            kind = str(d.get("kind", "")).lower()
            if kind not in ("issue", "suggestion"):
                kind = "suggestion" if severity == "info" else "issue"
            title = str(d.get("title", "")).strip()
            if not title:
                return None
            return cls(
                category=str(d.get("category", "code-quality")),
                severity=severity,
                file=str(d.get("file") or default_file),
                line=int(d.get("line", 0) or 0),
                title=title,
                explanation=str(d.get("explanation", "")).strip(),
                evidence=str(d.get("evidence", "")).strip(),
                recommendation=str(d.get("recommendation", "")).strip(),
                kind=kind,
            )
        except (TypeError, ValueError):
            return None


def _extract_json_array(text: str):
    """Best-effort extraction of a JSON array from an LLM response."""
    if not text:
        return []
    # strip markdown fences if present
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    # direct parse
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        pass
    # fall back: grab the outermost [...] block
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            return []
    return []


def parse_findings(text: str, default_file: str = "") -> List[Finding]:
    out: List[Finding] = []
    for item in _extract_json_array(text):
        if isinstance(item, dict):
            f = Finding.from_dict(item, default_file=default_file)
            if f:
                out.append(f)
    return out


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", t.lower()).strip()


def dedupe(findings: List[Finding]) -> List[Finding]:
    """Drop near-duplicate findings, keeping the highest-severity copy.

    Two findings collide when they share file, a ~5-line proximity bucket, and a
    normalized title.
    """
    best: dict = {}
    for f in findings:
        key = (f.file, f.line // 5, _norm_title(f.title))
        cur = best.get(key)
        if cur is None or SEVERITY_WEIGHT.get(f.severity, 0) > SEVERITY_WEIGHT.get(cur.severity, 0):
            best[key] = f
    return list(best.values())


def sort_by_severity(findings: List[Finding]) -> List[Finding]:
    return sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))


def severity_counts(findings: List[Finding]) -> dict:
    counts = {s: 0 for s in SEVERITY_WEIGHT}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts
