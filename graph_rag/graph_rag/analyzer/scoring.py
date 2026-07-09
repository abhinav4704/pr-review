"""Stage 3 — propagate + score (plan.md §3). Finds nothing new; sorts the pile
Stages 1-2 produced. Three questions per finding, in order:

  1. blast_radius — who calls the owning function, at all? Pure Cypher walk,
     no LLM.
  2. qualify      — of those callers, how many pass genuinely dangerous input
     vs. hardcoded/guarded args? One batched LLM call per finding. Skippable
     (`--no-llm` / no llm passed) — falls back to raw blast_count, per the
     plan's own open decision in §7.
  3. severity     — pure formula, zero LLM, reproducible by construction.
"""
from __future__ import annotations

from ..graph_core.findings import SEVERITY_BASE, DEFAULT_SEVERITY_BASE, Finding
from ..graph_core.store import GraphStore

_BLAST_MAX_HOPS = 4


def blast_radius(store: GraphStore, repo: str, owning_fqn: str, max_hops: int = _BLAST_MAX_HOPS) -> list[dict]:
    """All distinct functions that can reach `owning_fqn` via CALLS, up to
    max_hops. Pure Cypher, no LLM."""
    rows = store.read(
        f"MATCH (start:Function {{repo:$repo, fqn:$fqn}}) "
        f"MATCH p = (caller:Function)-[:CALLS*1..{int(max_hops)}]->(start) "
        f"WHERE caller <> start "
        f"RETURN DISTINCT caller.id AS id, caller.fqn AS fqn, caller.file AS file, "
        f"caller.signature AS signature, caller.docstring AS docstring, "
        f"caller.component_role AS component_role",
        repo=repo, fqn=owning_fqn,
    )
    return rows


_QUALIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "dangerous_callers": {
            "type": "array",
            "description": "fqns from the given caller list that pass genuinely "
                            "attacker-influenced or otherwise dangerous input, as "
                            "opposed to hardcoded/constant/already-guarded args.",
            "items": {"type": "string"},
        }
    },
    "required": ["dangerous_callers"],
}

_QUALIFY_SYSTEM = (
    "You are pruning a blast-radius list for one finding. Given the finding and a "
    "list of callers (fqn + signature + docstring), return only the fqns of callers "
    "that plausibly pass dangerous/attacker-influenced input to this finding's "
    "location — not ones that pass hardcoded constants or already-validated values."
)


def qualify(store: GraphStore, repo: str, finding: Finding, llm=None,
            callers: list[dict] | None = None) -> int:
    """Returns the qualified (real) blast count. With no llm, falls back to the
    raw blast_count (plan.md §7 open decision — rank on raw count if the call
    isn't worth it)."""
    if callers is None:
        callers = blast_radius(store, repo, finding.owning_fqn)
    finding.blast_count = len(callers)
    if not callers:
        finding.qualified_blast_count = 0
        return 0
    if llm is None:
        finding.qualified_blast_count = finding.blast_count
        return finding.qualified_blast_count

    caller_desc = "\n".join(
        f"- {c['fqn']}: signature={c.get('signature') or ''} docstring={c.get('docstring') or ''}"
        for c in callers
    )
    user = (
        f"finding: [{finding.subcategory}] {finding.message}\n"
        f"location: {finding.owning_fqn} ({finding.file}:{finding.line})\n\n"
        f"callers:\n{caller_desc}\n"
    )
    try:
        result = llm.extract(_QUALIFY_SYSTEM, user, _QUALIFY_SCHEMA)
    except Exception:
        finding.qualified_blast_count = finding.blast_count
        return finding.qualified_blast_count
    dangerous = result.get("dangerous_callers", []) if isinstance(result, dict) else []
    finding.qualified_blast_count = min(len(dangerous), finding.blast_count) if dangerous else 0
    return finding.qualified_blast_count


def severity(finding: Finding, endpoint_reachable: bool) -> float:
    """severity = f(base, endpoint_reachable, qualified_blast_count).

    Base selection:
      - graph_proven (deterministic, known taxonomy) → SEVERITY_BASE table.
      - llm_judged with an LLM-assigned severity → that value (the table can't
        know a free-form subcategory's weight; the reviewer that read the code
        does). Falls back to the table, then DEFAULT, if the LLM gave none.
    Multipliers are still pure formula — same inputs, same number every run."""
    if finding.source == "graph_proven":
        base = SEVERITY_BASE.get(finding.subcategory, DEFAULT_SEVERITY_BASE)
    elif finding.llm_severity > 0:
        base = finding.llm_severity
    else:
        base = SEVERITY_BASE.get(finding.subcategory, DEFAULT_SEVERITY_BASE)
    reach_factor = 1.0 if endpoint_reachable else 0.6
    blast_factor = 1.0
    if finding.blast_count > 0:
        blast_factor = 0.5 + 0.5 * min(finding.qualified_blast_count / finding.blast_count, 1.0)
    elif finding.category == "correctness":
        # No known callers isn't automatically safe for an intra-fn bug (it can
        # still fire the first time this function runs) — don't zero it out.
        blast_factor = 0.8
    score = base * reach_factor * blast_factor
    return max(0.0, min(score, 10.0))


def severity_label(score: float) -> str:
    if score >= 8.0:
        return "CRITICAL"
    if score >= 6.0:
        return "HIGH"
    if score >= 3.0:
        return "MEDIUM"
    return "LOW"


def score_findings(store: GraphStore, repo: str, findings: list[Finding], llm=None) -> list[Finding]:
    """Runs blast + qualify + severity for every finding and returns them
    sorted worst-first. Mutates and returns the same Finding objects."""
    endpoint_fqns = {
        r["fqn"] for r in store.read(
            "MATCH (n:Function {repo:$repo, component_role:'endpoint_handler'}) "
            "RETURN n.fqn AS fqn",
            repo=repo,
        )
    }
    for f in findings:
        callers = blast_radius(store, repo, f.owning_fqn)
        qualify(store, repo, f, llm=llm, callers=callers)
        endpoint_reachable = f.owning_fqn in endpoint_fqns or any(
            c["fqn"] in endpoint_fqns for c in callers
        )
        f.severity = severity(f, endpoint_reachable)
        f.severity_label = severity_label(f.severity)

    return sorted(findings, key=lambda f: f.severity, reverse=True)
