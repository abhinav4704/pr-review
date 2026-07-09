"""Shared finding model for Stage 2 (Agent 1/2) and Stage 3 (scoring).

Severity vs. confidence are deliberately separate axes (plan.md §11.1):
    severity   — how bad if it's real. Stage 3 computes the final printed number
                 from a BASE severity times blast/reachability multipliers.
                 The base comes from SEVERITY_BASE for deterministic graph_proven
                 classes (known taxonomy); for free-form llm_judged findings the
                 LLM supplies the base directly (`llm_severity`), since the fixed
                 table can't know the weight of a subcategory it's never seen.
                 The formula still owns the multipliers — an agent can propose how
                 bad the class is, but not how loud it finally prints.
    confidence — how sure we are it's real. Set by whichever pass produced the
                 finding; `source` says WHY that confidence should be trusted.

finding_id is stable and body-line-based on purpose (owning_fqn + category +
subcategory + line) so suppression/baseline (plan.md §11.2, not built yet) has
something durable to key off later — get this right now, don't migrate later.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

SOURCE_KINDS = {"graph_proven", "llm_judged", "llm_flagged_candidate"}
CONFIDENCE_LEVELS = {"HIGH", "MEDIUM", "LOW"}

# Pure-formula severity base, 0-10, per category/subcategory (plan.md §3 Stage 3).
# Deliberately conservative on the LLM-judged classes (plan.md §11.3/§11.6 —
# these are the noisiest; a formula, not a vibe, decides how loud they print).
SEVERITY_BASE = {
    "sql_injection": 9.0,
    "command_injection": 9.0,
    "deserialization": 8.0,
    "path_traversal": 7.0,
    "eval_injection": 8.0,
    "ssrf": 7.0,
    "template_injection": 7.0,
    "missing_authorization": 8.0,
    "resource_double_release": 6.0,
    "race_condition": 5.0,
    "unhandled_empty": 5.0,
    "bad_error_handling": 3.0,
    "breakage": 6.0,
    "layering_violation": 2.0,
    "circular_architectural_dependency": 3.0,
    # pre-existing gap: emitted directly by architecture.py's deterministic_findings()
    # but had no base here, so it silently fell back to the default.
    "chain_too_deep": 2.0,
    # taint sink classes added when the sink catalogue was expanded — same class
    # of gap as chain_too_deep above (emitted subcategory, no severity base yet).
    "xss": 7.0,
    "open_redirect": 5.0,
    "nosql_injection": 9.0,
    "ldap_injection": 8.0,
    "xxe": 8.0,
    "sensitive_data_exposure": 6.0,
    "crypto_misuse": 7.0,
    # pre-existing gap: these were already in agents.py's _CORRECTNESS_SUBCATS
    # enum but had no base here, so they silently fell back to the default.
    "null_deref": 6.0,
    "off_by_one": 4.0,
    # merged Agent 1 single-file taxonomy — non-taint security
    "hardcoded_secret": 8.0,
    "weak_crypto": 6.0,
    "insecure_randomness": 5.0,
    "insecure_config": 6.0,
    "redos_vulnerable_regex": 5.0,
    "sensitive_data_logged": 6.0,
    # merged Agent 1 single-file taxonomy — correctness / error handling
    "incorrect_comparison": 3.0,
    "mutable_default_argument": 3.0,
    "incorrect_boolean_logic": 5.0,
    "type_confusion": 5.0,
    "unreachable_code": 1.0,
    "bare_except": 3.0,
    "swallowed_exception": 4.0,
    "silent_data_loss": 5.0,
    # merged Agent 1 single-file taxonomy — resource/reliability
    "resource_leak": 5.0,
    "missing_timeout": 3.0,
    "unbounded_retry": 3.0,
    # merged Agent 1 single-file taxonomy — performance ("unoptimizations")
    "n_plus_one_query": 4.0,
    "blocking_call_in_loop": 4.0,
    "quadratic_algorithm": 3.0,
    "inefficient_string_concat": 2.0,
    "redundant_recomputation": 2.0,
    "unnecessary_full_materialization": 2.0,
    "repeated_compile_in_loop": 2.0,
}
DEFAULT_SEVERITY_BASE = 4.0


def finding_id(owning_fqn: str, category: str, subcategory: str, line: int) -> str:
    raw = f"{owning_fqn}\x00{category}\x00{subcategory}\x00{line}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


@dataclass
class Finding:
    category: str            # security | correctness | impact | design
    subcategory: str         # sql_injection | unhandled_empty | resource_double_release | ...
    source: str              # graph_proven | llm_judged | llm_flagged_candidate
    owning_fqn: str
    file: str
    line: int
    message: str
    evidence: str = ""
    recommendation: str = ""
    confidence: str = "MEDIUM"     # this pass's confidence the finding is real
    llm_severity: float = 0.0      # base severity the LLM assigned (0 = not set)
    severity: float = 0.0          # filled by Stage 3; 0 until scored
    severity_label: str = ""       # CRITICAL|HIGH|MEDIUM|LOW, filled by Stage 3
    blast_count: int = 0
    qualified_blast_count: int = 0
    id: str = field(default="", init=False)

    def __post_init__(self):
        if self.confidence not in CONFIDENCE_LEVELS:
            self.confidence = "MEDIUM"
        if self.source not in SOURCE_KINDS:
            self.source = "llm_judged"
        self.id = finding_id(self.owning_fqn, self.category, self.subcategory, self.line)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "subcategory": self.subcategory,
            "source": self.source,
            "owning_fqn": self.owning_fqn,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "llm_severity": round(self.llm_severity, 2),
            "severity": round(self.severity, 2),
            "severity_label": self.severity_label,
            "blast_count": self.blast_count,
            "qualified_blast_count": self.qualified_blast_count,
        }

    @classmethod
    def from_dict(cls, d: dict, *, category: str, source: str,
                  owning_fqn: str, file: str) -> "Finding | None":
        message = str(d.get("message") or d.get("title") or "").strip()
        if not message:
            return None
        try:
            line = int(d.get("line", 0) or 0)
        except (TypeError, ValueError):
            line = 0
        confidence = str(d.get("confidence", "MEDIUM")).upper()
        return cls(
            category=category,
            subcategory=str(d.get("subcategory", "")).strip() or "unspecified",
            source=source,
            owning_fqn=str(d.get("owning_fqn") or owning_fqn),
            file=str(d.get("file") or file),
            line=line,
            message=message,
            evidence=str(d.get("evidence", "")).strip(),
            recommendation=str(d.get("recommendation", "")).strip(),
            confidence=confidence if confidence in CONFIDENCE_LEVELS else "MEDIUM",
            llm_severity=_parse_severity(d.get("severity")),
        )


# Band labels an LLM may emit instead of a number; mapped to the middle of each
# band so the scoring formula's multipliers still have room to move it.
_SEVERITY_BANDS = {
    "CRITICAL": 9.0, "HIGH": 7.0, "MEDIUM": 5.0, "MED": 5.0, "LOW": 2.5, "INFO": 1.0,
}


def _parse_severity(raw) -> float:
    """Coerce an LLM-supplied severity (number 0-10 or band label) to a float.
    Returns 0.0 when absent/unparseable, meaning 'no LLM base — fall back'."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return max(0.0, min(float(raw), 10.0))
    band = str(raw).strip().upper()
    if band in _SEVERITY_BANDS:
        return _SEVERITY_BANDS[band]
    try:
        return max(0.0, min(float(band), 10.0))
    except ValueError:
        return 0.0


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Keep the first occurrence per stable id (owning-fqn based) — the
    free deterministic safety net plan.md §Stage2 calls for, for the ~10-20%
    prompt-rule leakage across overlapping bundles."""
    out: list[Finding] = []
    seen: set[str] = set()
    for f in findings:
        if f.id in seen:
            continue
        seen.add(f.id)
        out.append(f)
    return out


# Same underlying bug class, re-detected independently by an LLM pass under a
# different top-level category than the deterministic/graph-proven pass that
# already caught it. `dedupe()` above can't catch these since its id includes
# category, and category is exactly what differs here (e.g. Stage 1 emits
# `security/sql_injection`, graph_proven; Agent A independently re-guesses the
# same sink as `correctness/sql_injection`, llm_judged, on the same function) —
# found live: this doubled the finding count for every real SQLi bug in the
# fixture without adding any new signal.
_CROSS_CATEGORY_SUBCATS = {"sql_injection", "command_injection"}


def collapse_cross_category_duplicates(findings: list[Finding]) -> list[Finding]:
    """Drop an `llm_judged`/non-`graph_proven` finding when a `graph_proven`
    finding already exists for the same (owning_fqn, subcategory) — the
    deterministic pass is authoritative for these subcategories; the LLM
    re-detecting the same sink independently is redundant, not corroborating
    (Agent B's qualify pass already provides the "is it actually reachable/
    sanitized" nuance for these)."""
    graph_proven_keys = {
        (f.owning_fqn, f.subcategory)
        for f in findings
        if f.source == "graph_proven" and f.subcategory in _CROSS_CATEGORY_SUBCATS
    }
    out: list[Finding] = []
    for f in findings:
        if (f.source != "graph_proven"
                and f.subcategory in _CROSS_CATEGORY_SUBCATS
                and (f.owning_fqn, f.subcategory) in graph_proven_keys):
            continue
        out.append(f)
    return out
