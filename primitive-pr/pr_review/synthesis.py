"""Track C — Per-cluster Issue Synthesizer.

After Track A (per-file findings) and Track B (impact-chain findings) each do
their independent jobs, this module runs a third LLM pass *per cluster* that:

  1. Matches Track A findings to the cluster by file + line range.
  2. Collects Track B impact findings + chain text from the ClusterReview.
  3. Sends both to a "security review lead" LLM persona that merges them into
     one or more IssueReport objects — coherent vulnerability narratives with
     root cause, exploit path, blast radius, and confidence.

The output is a list of IssueReport objects (one per issue, not per raw finding).
Humans read claims, not disconnected finding lists.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from .findings import Finding, SEVERITY_ORDER, SEVERITY_WEIGHT, sort_by_severity
from .impact import Cluster, chain_to_text, chain_locations
from .pr_passes import ClusterReview

# CompleteFn: fn(system_prompt, user_prompt) -> str
CompleteFn = Callable[[str, str], str]

DEFAULT_BUDGET = 12000

# ── output schema ─────────────────────────────────────────────────────────────

ISSUE_SCHEMA = """Reply with ONLY a JSON array of issue objects (no extra text, no markdown).
Each item follows this schema exactly:
{
  "title":               string,   // one short imperative sentence naming the issue
  "severity":            "critical" | "high" | "medium" | "low",
  "root_cause_file":     string,   // file path where the change originates
  "root_cause_line":     integer,  // most relevant changed line (0 if unsure)
  "root_cause_summary":  string,   // 2-3 sentences: what changed and why it is dangerous
  "exploit_path":        string,   // numbered step-by-step attacker/caller narrative
  "impact_chain":        string,   // "file1 → file2 → file3" blast-radius text
  "affected_files":      [string], // every file reachable in the blast radius
  "recommendation":      string,   // concrete fix, one paragraph
  "confidence":          "high" | "medium" | "low"
}

Rules:
- Use "critical" only when your confidence is "high" — a critical you are unsure about
  will be trimmed to high. Confident criticals of any kind are kept.
- Merge overlapping findings and chains into a single issue — do NOT list them separately.
- If findings reference the same root cause, produce ONE issue, not N.
- If there is truly more than one independent issue, produce separate objects.
- If nothing rises to the level of a real issue (all noise), return [].
- Do not invent problems not evidenced by the findings or chains shown.
"""

SYNTHESIZER_SYSTEM = (
    "You are a security review lead. You receive two inputs for a single cluster of "
    "co-changed code:\n"
    "  A. LOCAL FINDINGS — issues identified in the changed files directly (bugs, "
    "     vulnerabilities, injection risks, auth bypasses, etc.).\n"
    "  B. IMPACT CHAINS — deterministic call-graph propagation showing which unchanged "
    "     consumers are reached by the change, and whether any breakage was detected "
    "     in those consumers.\n\n"
    "Your job: synthesize A and B into a unified security narrative for each real issue.\n"
    "For each issue you produce:\n"
    "  1. State the root cause (the changed line that introduces the problem).\n"
    "  2. Cite changed code (file + line).\n"
    "  3. Explain exploitability (who triggers it and how).\n"
    "  4. Incorporate the blast radius from the impact chains.\n"
    "  5. Give a concrete recommendation.\n\n"
    "Do NOT produce one paragraph per finding. Merge findings that share a root cause.\n\n"
    + ISSUE_SCHEMA
)


# ── IssueReport ───────────────────────────────────────────────────────────────

@dataclass
class IssueReport:
    """A synthesized vulnerability / issue claim combining Track A + Track B evidence."""
    title: str
    severity: str                        # critical / high / medium / low
    root_cause_file: str
    root_cause_line: int
    root_cause_summary: str
    exploit_path: str                    # step-by-step narrative
    impact_chain: str                    # "A → B → C" blast-radius text
    affected_files: List[str]
    recommendation: str
    confidence: str                      # high / medium / low
    evidence_by_file: Dict[str, List[str]] = field(default_factory=dict)
    source_finding_ids: List[str] = field(default_factory=list)
    unassigned_ids: List[str] = field(default_factory=list)
    is_unassigned_bucket: bool = False
    cluster_idx: int = 0                 # which cluster produced this
    source_findings: List[Finding] = field(default_factory=list)  # Track A + B inputs merged in

    @classmethod
    def from_dict(cls, d: dict, cluster_idx: int = 0,
                  source_findings: Optional[List[Finding]] = None) -> Optional["IssueReport"]:
        try:
            title = str(d.get("title", "")).strip()
            if not title:
                return None
            severity = str(d.get("severity", "medium")).lower()
            if severity not in SEVERITY_WEIGHT:
                severity = "medium"
            confidence = str(d.get("confidence", "medium")).lower()
            if confidence not in ("high", "medium", "low"):
                confidence = "medium"
            affected = d.get("affected_files") or []
            if isinstance(affected, str):
                affected = [s.strip() for s in affected.split(",") if s.strip()]
            return cls(
                title=title,
                severity=severity,
                root_cause_file=str(d.get("root_cause_file", "") or ""),
                root_cause_line=int(d.get("root_cause_line", 0) or 0),
                root_cause_summary=str(d.get("root_cause_summary", "") or "").strip(),
                exploit_path=str(d.get("exploit_path", "") or "").strip(),
                impact_chain=str(d.get("impact_chain", "") or "").strip(),
                affected_files=list(affected),
                recommendation=str(d.get("recommendation", "") or "").strip(),
                confidence=confidence,
                evidence_by_file={},
                source_finding_ids=[],
                unassigned_ids=[],
                is_unassigned_bucket=False,
                cluster_idx=cluster_idx,
                source_findings=source_findings or [],
            )
        except (TypeError, ValueError):
            return None


def finding_id(f: Finding) -> str:
    """Stable synthetic identifier used for provenance/no-loss accounting."""
    title = re.sub(r"\s+", " ", (f.title or "").strip().lower())
    ev = re.sub(r"\s+", " ", (f.evidence or "").strip().lower())[:80]
    return f"{f.file}:{f.line}:{f.severity}:{title}:{ev}"


def _evidence_by_file(findings: List[Finding]) -> Dict[str, List[str]]:
    by_file: Dict[str, List[str]] = {}
    for f in findings:
        path = f.file or "unknown"
        line = f"line {f.line}" if f.line else "line ?"
        text = (f.evidence or f.explanation or f.title or "").strip()
        if not text:
            continue
        entry = f"{line}: {text}"
        cur = by_file.setdefault(path, [])
        if entry not in cur:
            cur.append(entry)
    return by_file


def _parse_issue_reports(text: str, cluster_idx: int,
                         source_findings: List[Finding]) -> List[IssueReport]:
    """Extract IssueReport objects from an LLM response."""
    if not text:
        return []
    # strip markdown fences
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    # direct parse
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return []
    if not isinstance(data, list):
        if isinstance(data, dict):
            data = [data]
        else:
            return []
    out: List[IssueReport] = []
    for item in data:
        if isinstance(item, dict):
            r = IssueReport.from_dict(item, cluster_idx=cluster_idx,
                                      source_findings=source_findings)
            if r:
                out.append(r)
    return out


# ── matching Track A findings → cluster ───────────────────────────────────────

def _findings_for_cluster(cg, cluster: Cluster,
                           track_a_results: Dict[str, List[Finding]]) -> List[Finding]:
    """Return Track A findings whose file + line overlaps any member of the cluster.

    Matching priority:
      1. file == member_path AND start_line <= finding.line <= end_line
      2. fallback when finding.line == 0: file == member_path
    """
    matched: List[Finding] = []
    seen_ids: set = set()

    for nid in cluster.members:
        if not cg.has(nid):
            continue
        d = cg.node(nid)
        member_path = d.get("path", "")
        start = d.get("start_line", 0)
        end = d.get("end_line", 0)
        for f in track_a_results.get(member_path, []):
            fid = id(f)
            if fid in seen_ids:
                continue
            if f.line == 0:
                # file-only match — looser but better than nothing
                seen_ids.add(fid)
                matched.append(f)
            elif start <= f.line <= end:
                seen_ids.add(fid)
                matched.append(f)
    return sort_by_severity(matched)


# ── synthesis dossier ─────────────────────────────────────────────────────────

def _finding_to_compact(f: Finding) -> dict:
    return {
        "severity": f.severity,
        "category": f.category,
        "file": f.file,
        "line": f.line,
        "title": f.title,
        "explanation": f.explanation,
        "evidence": f.evidence,
        "recommendation": f.recommendation,
    }


def _build_synthesis_dossier(cg, cluster_review: ClusterReview,
                              matched_findings: List[Finding],
                              diff_by_file: Optional[Dict[str, str]] = None) -> str:
    """Build the compact dossier sent to the Synthesizer LLM.

    Intentionally does NOT re-send full consumer source (already consumed by
    Track B). Only sends: changed node identities, Track A JSON, chain text, and —
    when ``diff_by_file`` is supplied — the actual diff of the changed files so the
    synthesizer can see exactly which lines changed.
    """
    cluster = cluster_review.cluster
    parts: List[str] = ["# Synthesis input for a cluster of co-changed code\n"]

    # 1. Changed node identities (names + files + lines)
    parts.append("## Changed code locations\n")
    cluster_files: List[str] = []
    for nid in cluster.members:
        if not cg.has(nid):
            continue
        d = cg.node(nid)
        path = d.get("path", "?")
        if path not in cluster_files:
            cluster_files.append(path)
        parts.append(
            f"- **{d.get('kind', '?')} `{d.get('qualname') or d.get('name')}`** "
            f"in `{path}` lines {d.get('start_line')}–{d.get('end_line')}\n"
        )

    # 1b. The actual changed lines (diff), so Track C knows what changed.
    if diff_by_file:
        diff_blocks = [(p, diff_by_file[p]) for p in cluster_files
                       if diff_by_file.get(p, "").strip()]
        if diff_blocks:
            parts.append("\n## What changed (diff of the changed files)\n")
            for p, diff_text in diff_blocks:
                parts.append(f"`{p}`\n```diff\n{diff_text}\n```\n")

    # 2. Track A findings (local — in the changed files)
    parts.append("\n## Local findings (Track A — from direct file review)\n")
    if matched_findings:
        parts.append(
            "```json\n"
            + json.dumps([_finding_to_compact(f) for f in matched_findings], indent=2)
            + "\n```\n"
        )
    else:
        parts.append("_No local findings for this cluster._\n")

    # 3. Track B impact findings + chain text
    parts.append("\n## Impact findings (Track B — from propagation chains)\n")
    if cluster_review.findings:
        parts.append(
            "```json\n"
            + json.dumps([_finding_to_compact(f) for f in cluster_review.findings], indent=2)
            + "\n```\n"
        )
    else:
        parts.append("_No impact findings for this cluster._\n")

    parts.append("\n## Propagation chains (blast radius)\n")
    if cluster.chains:
        for i, ch in enumerate(cluster.chains, 1):
            loc = chain_locations(ch)
            txt = chain_to_text(ch)
            flags = []
            if ch.field_hits:
                flags.append(f"field hits: {', '.join(ch.field_hits)}")
            if ch.uncertain:
                flags.append("uncertain link")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            parts.append(f"{i}. {txt}{flag_str}\n   Locations: {loc}\n")
        if cluster.extra_consumers:
            parts.append(f"_(+{cluster.extra_consumers} more consumer(s) not shown)_\n")
    else:
        parts.append("_No propagation chains — change has no unchanged consumers in graph._\n")

    return "".join(parts)


# ── per-cluster synthesis ─────────────────────────────────────────────────────

def synthesize_cluster(
    cg,
    cluster_review: ClusterReview,
    track_a_results: Dict[str, List[Finding]],
    complete: CompleteFn,
    budget: int = DEFAULT_BUDGET,
    cluster_idx: int = 0,
    diff_by_file: Optional[Dict[str, str]] = None,
) -> List[IssueReport]:
    """Run the Synthesizer LLM for one cluster.

    Returns [] if neither Track A nor Track B produced any signals for this cluster.
    """
    cluster = cluster_review.cluster
    matched = _findings_for_cluster(cg, cluster, track_a_results)

    # Skip if both tracks came up empty — no signal to synthesize
    if not matched and not cluster_review.findings and not cluster.chains:
        return []

    dossier = _build_synthesis_dossier(cg, cluster_review, matched, diff_by_file)

    # Trim to budget (keep the synthesis input whole — no chunking; the dossier
    # is already much smaller than the full cluster dossier in pr_passes)
    if len(dossier) > budget:
        dossier = dossier[:budget] + "\n…(dossier trimmed to budget)…"

    raw = complete(SYNTHESIZER_SYSTEM, dossier)
    all_source = sort_by_severity(matched + cluster_review.findings)
    reports = _parse_issue_reports(raw, cluster_idx=cluster_idx, source_findings=all_source)
    ids = sorted({finding_id(f) for f in all_source})
    evidence = _evidence_by_file(all_source)
    for r in reports:
        r.source_finding_ids = ids
        r.evidence_by_file = dict(evidence)
    return reports


# ── global synthesis across all clusters ─────────────────────────────────────

def _severity_key(r: IssueReport) -> int:
    return SEVERITY_ORDER.get(r.severity, 99)


def finalize_report_severity(r: IssueReport) -> None:
    """Light guard on a synthesized issue's "critical": keep it unless low-confidence.

    The synthesizer's severity is trusted; the only trim is a "critical" the model is
    not highly confident about, which drops to high. Never upgrades.
    """
    if r.severity == "critical" and r.confidence != "high":
        r.severity = "high"


def _dedupe_reports(reports: List[IssueReport]) -> List[IssueReport]:
    """Drop near-duplicate IssueReports (same file + 5-line bucket + normalized title)."""
    def _norm(t: str) -> str:
        return re.sub(r"\s+", " ", t.lower()).strip()

    best: Dict[Tuple, IssueReport] = {}
    for r in reports:
        key = (r.root_cause_file, r.root_cause_line // 5, _norm(r.title))
        if key not in best:
            best[key] = r
            continue
        # Keep highest severity report and merge provenance/evidence so nothing is lost.
        keep = best[key]
        drop = r
        if _severity_key(r) < _severity_key(keep):
            keep, drop = r, keep
        keep.source_findings = sort_by_severity(keep.source_findings + drop.source_findings)
        keep.source_finding_ids = sorted(set(keep.source_finding_ids + drop.source_finding_ids))
        for path, items in drop.evidence_by_file.items():
            cur = keep.evidence_by_file.setdefault(path, [])
            for it in items:
                if it not in cur:
                    cur.append(it)
        best[key] = keep
    return sorted(best.values(), key=_severity_key)


def _build_unassigned_issue(unused_findings: List[Finding]) -> IssueReport:
    """Create an explicit bucket issue so unmerged findings are still visible."""
    sev_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
    worst = "info"
    for f in unused_findings:
        if sev_rank.get(f.severity, 0) > sev_rank.get(worst, 0):
            worst = f.severity
    title = "Unassigned Findings"
    summary = (
        "Some findings were not attached to a synthesized issue. "
        "They are listed here explicitly to preserve recall and avoid information loss."
    )
    rec = "Review these findings manually and either map them to an existing issue or open a new issue."
    ids = sorted({finding_id(f) for f in unused_findings})
    return IssueReport(
        title=title,
        severity=worst if worst in SEVERITY_WEIGHT else "medium",
        root_cause_file="",
        root_cause_line=0,
        root_cause_summary=summary,
        exploit_path="",
        impact_chain="",
        affected_files=sorted({f.file for f in unused_findings if f.file}),
        recommendation=rec,
        confidence="high",
        evidence_by_file=_evidence_by_file(unused_findings),
        source_finding_ids=ids,
        unassigned_ids=ids,
        is_unassigned_bucket=True,
        cluster_idx=-1,
        source_findings=sort_by_severity(unused_findings),
    )


def _with_no_loss_accounting(
    reports: List[IssueReport],
    all_inputs: List[Finding],
) -> List[IssueReport]:
    all_ids: Set[str] = {finding_id(f) for f in all_inputs}
    used_ids: Set[str] = set()
    for r in reports:
        if not r.source_finding_ids:
            r.source_finding_ids = sorted({finding_id(f) for f in r.source_findings})
        used_ids |= set(r.source_finding_ids)
    missing = all_ids - used_ids
    if not missing:
        return reports
    by_id = {finding_id(f): f for f in all_inputs}
    unassigned = [by_id[i] for i in sorted(missing) if i in by_id]
    if unassigned:
        reports = reports + [_build_unassigned_issue(unassigned)]
    return sorted(reports, key=_severity_key)


def synthesize_all(
    cg,
    cluster_reviews: List[ClusterReview],
    track_a_results: Dict[str, List[Finding]],
    complete: CompleteFn,
    budget: int = DEFAULT_BUDGET,
    max_workers: int = 4,
    progress_cb=None,
    diff_by_file: Optional[Dict[str, str]] = None,
) -> List[IssueReport]:
    """Synthesize all clusters concurrently; return deduplicated IssueReports sorted by severity.

    Mirrors the ThreadPoolExecutor pattern of review_pr_impact().

    ``diff_by_file`` (optional) lets each cluster's dossier include the actual diff of
    its changed files, so Track C sees which lines changed.
    """
    total = len(cluster_reviews)
    if not total:
        return []

    results: List[Optional[List[IssueReport]]] = [None] * total
    workers = max(1, min(max_workers, total))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(
                synthesize_cluster, cg, cr, track_a_results, complete, budget, idx,
                diff_by_file
            ): idx
            for idx, cr in enumerate(cluster_reviews)
        }
        done = 0
        for fut in as_completed(futs):
            idx = futs[fut]
            try:
                results[idx] = fut.result()
            except Exception:
                results[idx] = []
            done += 1
            if progress_cb:
                progress_cb(done - 1, total, "")

    if progress_cb:
        progress_cb(total, total, "")

    all_reports: List[IssueReport] = [
        r for sub in results if sub for r in sub
    ]
    deduped = _dedupe_reports(all_reports)

    # No-loss guarantee: every Track A/B input finding must appear in some issue,
    # otherwise it is surfaced in an explicit Unassigned Findings bucket.
    all_inputs: List[Finding] = []
    for cr in cluster_reviews:
        all_inputs.extend(cr.findings)
        all_inputs.extend(_findings_for_cluster(cg, cr.cluster, track_a_results))
    reports = _with_no_loss_accounting(deduped, all_inputs)
    for r in reports:
        finalize_report_severity(r)
    return sorted(reports, key=_severity_key)
