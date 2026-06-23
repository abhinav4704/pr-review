"""LLM PR review in two complementary tracks.

  A. Per-file whole-file pass (``review_pr``) — the secondary "is every file
     correct / any suggestions" sweep: breakage, exposed secrets/keys,
     optimization, using the diff + full file as context.

  B. Impact-chain pass (``review_pr_impact``) — the PRIMARY function. It groups
     co-changed functions into clusters (see ``impact``) and, for each cluster,
     hands the LLM ONE dossier: the changed source, the deterministic
     propagation chains (source -> ... -> unchanged consumer), and the source of
     each unchanged consumer — then asks "which consumers does this change
     actually break, and why". This is what surfaces the
     "this change -> caused this -> broke these" story that a per-node,
     one-hop-at-a-time review can't assemble.

Each pass asks the model for JSON findings (see findings.FINDINGS_SCHEMA).
Oversized inputs are split into chunks and the findings merged.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .diff import old_signature_for
from .findings import (
    FINDINGS_SCHEMA,
    Finding,
    dedupe,
    parse_findings,
    sort_by_severity,
)
from .impact import (
    Chain,
    ChainNode,
    Cluster,
    ImpactResult,
    analyze_impact,
    chain_to_text,
    gone_fields,
)

# A completion function: fn(system, user) -> str
CompleteFn = Callable[[str, str], str]

DEFAULT_BUDGET = 12000   # char budget per LLM call before chunking


# ── source reading ───────────────────────────────────────────────────────────
def read_source(src_path: str, file_path: str, start_line: int, end_line: int) -> str:
    full = Path(src_path) / file_path
    if not full.exists():
        return ""
    lines = full.read_text(errors="replace").splitlines()
    return "\n".join(lines[max(0, start_line - 1):end_line])


def read_file(src_path: str, file_path: str) -> str:
    full = Path(src_path) / file_path
    if not full.exists():
        return ""
    return full.read_text(errors="replace")


def _chunk(text: str, budget: int) -> List[str]:
    """Split text on line boundaries into <=budget-char chunks."""
    if len(text) <= budget:
        return [text]
    chunks: List[str] = []
    cur: List[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > budget and cur:
            chunks.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        chunks.append("".join(cur))
    return chunks


# ── system prompts ───────────────────────────────────────────────────────────
WHOLE_FILE_SYSTEM = (
    "You are AGENT 1 — the changed-file reviewer. Your only job is to analyse the file that "
    "was changed in this PR.\n"
    "You receive: the DIFF (lines that changed) and the FULL FILE for context.\n\n"
    "YOUR SCOPE — report only:\n"
    "- Bugs introduced by the diff (logic errors, null dereferences, wrong conditions).\n"
    "- Security problems in the changed code (exposed secrets = critical; SQL injection, "
    "unchecked input, missing auth = high/medium).\n"
    "- Performance problems added by the diff.\n"
    "- Improvements to the changed lines only.\n\n"
    "OUT OF SCOPE — do NOT report:\n"
    "- Whether any OTHER file or caller breaks because of this change. That is Agent 2's job.\n"
    "- Problems that existed before this diff and are unrelated to the changed lines.\n"
    "- Any finding whose 'file' field is not the file you are reviewing right now.\n\n"
    "Read the full file only to understand context. All findings must cite lines inside "
    "this file.\n\n" + FINDINGS_SCHEMA
)

IMPACT_CHAIN_SYSTEM = (
    "You are checking what a code change might break. You are given three things:\n"
    "1. The CHANGED FUNCTIONS (where the change starts). Every line has a number on the left; "
    "lines marked '>' are the ones that changed.\n"
    "2. CHAINS showing which CALLERS depend on the changed code. Format: "
    "`changed_function → ... → caller`. The arrow means 'is called by' — the leftmost entry "
    "is the changed function; the rightmost entry is the CALLER (consumer). "
    "The actual call direction in the code is REVERSED: caller → changed_function. "
    "Do NOT interpret the arrow as showing that the changed function calls the consumer.\n"
    "3. The full code of each CALLER (consumer) at the end of a chain, with line numbers. "
    "These callers were NOT changed in this PR.\n\n"
    "For each chain, ask one question: does the change actually break this caller? It breaks "
    "the caller if it still depends on something the change altered — for example a "
    "renamed or removed field, a different return value or type, changed arguments, or a new "
    "error. If it breaks, report category 'breaking' (critical) and:\n"
    "- set \"file\" to the CALLER's file and \"line\" to the exact line in that caller that "
    "breaks (use the line numbers shown in the left margin — do not guess).\n"
    "- put the broken line of code in \"evidence\".\n"
    "- in \"explanation\", name the cause: which changed function (and its file) broke it, e.g. "
    "'make_user (schema.py) renamed age->years, so show (app.py:5) still reads p[\"age\"] and "
    "will crash'.\n"
    "If the chain exists but the caller does not actually use what changed, do not report it. "
    "Only report problems caused by the change.\n"
    "IMPORTANT: Set 'file' and 'line' to the exact file path and line number shown in the "
    "caller source header in section 3. Do not invent class names or method calls that are "
    "not visible in the source listing shown to you.\n\n" + FINDINGS_SCHEMA
)

VERIFY_SYSTEM = (
    "You are double-checking a list of review findings against the code. You get the DOSSIER "
    "(the changed code, the chains, and the consumer code) and a JSON list of FINDINGS. Keep "
    "only the findings the code clearly supports — where the named consumer really depends on "
    "what changed, so the problem is real. Drop anything that is a guess, too general, or not "
    "backed by the code shown. Do not add new findings and do not rewrite them — return the "
    "ones you keep exactly as they are. Reply with ONLY a JSON array (it can be empty)."
)


def annotate_changed(src: str, start_line: int, added_lines) -> str:
    """Prefix lines whose new-side number is in added_lines with '>>>' markers."""
    if not added_lines:
        return src
    out = []
    for offset, line in enumerate(src.splitlines()):
        lineno = start_line + offset
        out.append(f">>> {line}" if lineno in added_lines else f"    {line}")
    return "\n".join(out)


def number_lines(src: str, start_line: int, added_lines=None) -> str:
    """Show real line numbers in the left margin so the LLM can cite exact lines.

    Changed lines (in ``added_lines``) get a '>' marker; everything else a space.
        12 >| def make_user(name):
        13  |     return name
    """
    added = added_lines or set()
    out = []
    for offset, line in enumerate(src.splitlines()):
        lineno = start_line + offset
        mark = ">" if lineno in added else " "
        out.append(f"{lineno:>5} {mark}| {line}")
    return "\n".join(out)


# ── pass 1: whole file ───────────────────────────────────────────────────────
def pass_whole_file(file_path: str, src_path: str, complete: CompleteFn,
                    budget: int = DEFAULT_BUDGET, diff_text: str = "") -> List[Finding]:
    source = read_file(src_path, file_path)
    if not source.strip():
        return []
    diff_block = f"## Diff (what changed)\n```diff\n{diff_text}\n```\n\n" if diff_text else ""
    findings: List[Finding] = []
    chunks = _chunk(source, budget)
    for i, chunk in enumerate(chunks):
        part = f" (part {i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
        user = (f"# File: {file_path}{part}\n\n{diff_block}"
                f"## Full file (context)\n```\n{chunk}\n```")
        findings += parse_findings(complete(WHOLE_FILE_SYSTEM, user), default_file=file_path)
    return findings


def _run_chunked(system: str, user: str, file_path: str,
                 complete: CompleteFn, budget: int) -> List[Finding]:
    findings: List[Finding] = []
    for chunk in _chunk(user, budget):
        findings += parse_findings(complete(system, chunk), default_file=file_path)
    return findings


# ── track A: per-file whole-file review (secondary correctness/suggestions) ────
def review_file(src_path: str, file_diff, complete: CompleteFn,
                budget: int = DEFAULT_BUDGET, diff_text: str = "") -> List[Finding]:
    findings = pass_whole_file(file_diff.path, src_path, complete, budget, diff_text=diff_text)
    # Keep only findings whose reported file matches the reviewed file.  The LLM
    # has no visibility into other files in whole-file mode, so any cross-file
    # finding is a hallucination (e.g. referencing a method it never saw).
    reviewed = file_diff.path.replace("\\", "/")
    reviewed_base = os.path.basename(reviewed)
    def _in_file(f: Finding) -> bool:
        fp = (f.file or "").replace("\\", "/")
        if not fp:
            return True
        return fp == reviewed or fp.endswith("/" + reviewed) or os.path.basename(fp) == reviewed_base
    findings = [f for f in findings if _in_file(f)]
    return sort_by_severity(dedupe(findings))


def review_pr(src_path: str, file_diffs, complete: CompleteFn,
              budget: int = DEFAULT_BUDGET, progress_cb=None,
              diff_by_file: Dict[str, str] = None) -> Dict[str, List[Finding]]:
    diff_by_file = diff_by_file or {}
    results: Dict[str, List[Finding]] = {}
    total = len(file_diffs)
    for i, fd in enumerate(file_diffs):
        if progress_cb:
            progress_cb(i, total, fd.path)
        results[fd.path] = review_file(src_path, fd, complete, budget,
                                       diff_text=diff_by_file.get(fd.path, ""))
    if progress_cb:
        progress_cb(total, total, "")
    return results


# ── track B: impact-chain review (primary) ────────────────────────────────────
MAX_CONSUMERS_IN_DOSSIER = 6   # consumers whose source is sent to the LLM per cluster


@dataclass
class ClusterReview:
    """A cluster's deterministic impact tree paired with the LLM's verdict."""
    cluster: Cluster
    findings: List[Finding] = field(default_factory=list)


def _added_lines_for(file_diffs, path: str):
    for fd in file_diffs:
        if fd.path == path:
            return fd.added_lines
    return set()


def _caller_function_blocks(cg, src_path: str, target_node_id: str,
                            max_callers: int = 5) -> List[str]:
    """Render inbound caller evidence as full caller function code blocks."""
    rows = cg.caller_evidence_lines(
        target_node_id,
        relations={"calls", "instantiates", "imports", "references", "uses"},
        include_ambiguous=False,
    )

    blocks: List[str] = []
    seen: set = set()
    for row in rows:
        caller_id = str(row.get("caller_id") or "")
        if not caller_id or caller_id in seen or not cg.has(caller_id):
            continue
        seen.add(caller_id)

        d = cg.node(caller_id)
        path = str(d.get("path") or "")
        start = int(d.get("start_line") or 0)
        end = int(d.get("end_line") or 0)
        if not path or start <= 0 or end < start:
            continue

        src = read_source(src_path, path, start, end)
        if not src.strip():
            continue

        label = d.get("qualname") or d.get("name") or caller_id
        relation = str(row.get("relation") or "")
        numbered = number_lines(src, start)
        blocks.append(
            f"   - {d.get('kind','?')} {label} ({path}, lines {start}-{end}) [{relation}]\n"
            f"```\n{numbered}\n```\n"
        )
        if len(blocks) >= max_callers:
            break

    return blocks


def _cluster_dossier(cg, src_path: str, cluster: Cluster, file_diffs,
                     diff_by_file: Dict[str, str]) -> str:
    """Changed source(s) + old→new contract + chains + unchanged-consumer source."""
    parts: List[str] = ["# Impact analysis for a cluster of co-changed functions\n"]

    # 1. the source(s) of the change, with old signature + line numbers (> = changed)
    parts.append("## Changed source (origin of the change)\n")
    for nid in cluster.members:
        d = cg.node(nid) if cg.has(nid) else {}
        path = d.get("path", "")
        src = read_source(src_path, path, d.get("start_line", 0), d.get("end_line", 0))
        if not src.strip():
            continue
        src = number_lines(src, d.get("start_line", 0),
                           _added_lines_for(file_diffs, path))
        old_sig = old_signature_for(diff_by_file.get(path, ""), cg, nid)
        contract = f"_Old declaration:_ `{old_sig}`\n\n" if old_sig else ""
        parts.append(f"### {d.get('kind','?')} {d.get('qualname') or d.get('name')} "
                     f"({path})  (line numbers shown; '>' = changed)\n{contract}"
                     f"```\n{src}\n```\n")

    # 2. the deterministic propagation chains (already ranked, capped)
    parts.append("## Propagation chains (changed code → … → CALLER) — arrow = 'is called by'\n")
    shown = cluster.chains[:MAX_CONSUMERS_IN_DOSSIER]
    for i, ch in enumerate(shown, 1):
        flag = f"   ⚠ still uses removed/renamed field(s): {', '.join(ch.field_hits)}" \
            if ch.field_hits else ""
        maybe = "   (uncertain link — confirm the consumer really calls into this)" \
            if ch.uncertain else ""
        parts.append(f"{i}. {chain_to_text(ch)}{flag}{maybe}\n")
        caller_blocks = _caller_function_blocks(cg, src_path, ch.source.node_id)
        if caller_blocks:
            parts.append("   caller evidence (full caller functions):\n")
            parts.extend(caller_blocks)
    if cluster.extra_consumers or len(cluster.chains) > len(shown):
        more = cluster.extra_consumers + (len(cluster.chains) - len(shown))
        parts.append(f"_(+{more} more consumer(s) not shown)_\n")

    # 3. full source of the unchanged consumers so the LLM can judge breakage
    parts.append("\n## Unchanged consumers — do they still match the new contract?\n")
    seen = set()
    for ch in shown:
        c = ch.consumer
        if c.node_id in seen:
            continue
        seen.add(c.node_id)
        src = number_lines(read_source(src_path, c.path, c.start_line, c.end_line),
                           c.start_line)
        notes = []
        if c.is_test:
            notes.append("covered by a test")
        if ch.field_hits:
            notes.append("references removed/renamed field(s): " + ", ".join(ch.field_hits))
        note = f"  ({'; '.join(notes)})" if notes else ""
        parts.append(f"### {c.kind} {c.qualname} ({c.path}, lines "
                     f"{c.start_line}-{c.end_line}){note}\n```\n{src}\n```\n")
    return "".join(parts)


def _finding_to_dict(f: Finding) -> dict:
    return {"kind": f.kind, "category": f.category, "severity": f.severity,
            "file": f.file, "line": f.line, "title": f.title,
            "explanation": f.explanation, "evidence": f.evidence,
            "recommendation": f.recommendation}


def verify_findings(complete: CompleteFn, dossier: str, findings: List[Finding],
                    default_file: str, budget: int = DEFAULT_BUDGET) -> List[Finding]:
    """Prune findings not grounded in the dossier (second, strict LLM pass).

    This is a SINGLE call — the findings JSON is always kept whole and next to the
    evidence (we trim the dossier if needed, never split it from the findings). It
    can only prune, never wipe: if the verifier returns nothing usable we keep the
    originals, so a flaky/empty verification can't silently delete real (often
    cross-file) breakages.
    """
    if not findings:
        return findings
    payload = json.dumps([_finding_to_dict(f) for f in findings], indent=2)
    suffix = (f"\n\n## Candidate findings to verify (return the ones that are real)\n"
              f"```json\n{payload}\n```")
    # keep the findings whole; trim only the dossier to fit the budget
    room = max(2000, budget - len(suffix) - 300)
    doss = dossier if len(dossier) <= room else dossier[:room] + "\n…(dossier truncated)…"
    raw = complete(VERIFY_SYSTEM, doss + suffix)

    if "[" not in raw:                       # no array at all -> keep originals
        return findings
    verified = parse_findings(raw, default_file=default_file)
    if not verified:                         # empty/ambiguous -> don't wipe, keep all
        return findings
    # match leniently: a finding survives if the verifier kept its title OR its file:line
    kept_titles = {_norm(f.title) for f in verified}
    kept_loc = {(f.file, f.line) for f in verified}
    result = [f for f in findings
              if _norm(f.title) in kept_titles or (f.file, f.line) in kept_loc]
    return result or verified


def _norm(t: str) -> str:
    return " ".join((t or "").lower().split())


def _cluster_default_file(cg, cluster: Cluster) -> str:
    if cluster.members and cg.has(cluster.members[0]):
        return cg.node(cluster.members[0]).get("path", "")
    return ""


def pass_impact_chains(cg, src_path: str, cluster: Cluster, file_diffs,
                       complete: CompleteFn, budget: int = DEFAULT_BUDGET,
                       diff_by_file: Dict[str, str] = None) -> List[Finding]:
    if not cluster.chains:
        return []
    dossier = _cluster_dossier(cg, src_path, cluster, file_diffs, diff_by_file or {})
    return _run_chunked(IMPACT_CHAIN_SYSTEM, dossier,
                        _cluster_default_file(cg, cluster), complete, budget)


def _review_cluster(cg, src_path: str, cluster: Cluster, file_diffs, diff_by_file,
                    complete: CompleteFn, budget: int, verify: bool) -> ClusterReview:
    """One cluster end to end: dossier -> findings -> (optional) verification."""
    dossier = _cluster_dossier(cg, src_path, cluster, file_diffs, diff_by_file)
    default_file = _cluster_default_file(cg, cluster)
    findings = sort_by_severity(dedupe(
        _run_chunked(IMPACT_CHAIN_SYSTEM, dossier, default_file, complete, budget)))
    if verify and findings:
        findings = sort_by_severity(
            verify_findings(complete, dossier, findings, default_file, budget))
    return ClusterReview(cluster=cluster, findings=findings)


def review_pr_impact(cg, src_path: str, file_diffs, complete: CompleteFn,
                     budget: int = DEFAULT_BUDGET, max_depth: int = 3,
                     diff_by_file: Dict[str, str] = None, verify: bool = True,
                     max_workers: int = 4, progress_cb=None) -> List[ClusterReview]:
    """Build impact clusters/chains, then get the LLM's breakage verdict per cluster.

    Clusters are reviewed concurrently (each is an independent set of LLM calls),
    which is where almost all the wall-time goes. The progress callback is only
    invoked from this (main) thread so it stays safe to update a Streamlit UI.
    """
    diff_by_file = diff_by_file or {}
    gone = gone_fields(diff_by_file)

    def _consumer_src(node: ChainNode) -> str:
        return read_source(src_path, node.path, node.start_line, node.end_line)

    result: ImpactResult = analyze_impact(
        cg, file_diffs, max_depth=max_depth,
        gone=gone, consumer_src_fn=_consumer_src,
        expand_same_class=True)

    actionable = [c for c in result.clusters if c.chains]
    total = len(actionable)
    reviews: List[Optional[ClusterReview]] = [None] * total
    if not total:
        return []

    workers = max(1, min(max_workers, total))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(_review_cluster, cg, src_path, cluster, file_diffs,
                      diff_by_file, complete, budget, verify): idx
            for idx, cluster in enumerate(actionable)
        }
        done = 0
        for fut in as_completed(futs):
            idx = futs[fut]
            reviews[idx] = fut.result()
            done += 1
            if progress_cb:
                name = (cg.node(actionable[idx].members[0]).get("name", "")
                        if actionable[idx].members else "")
                progress_cb(done - 1, total, name)
    if progress_cb:
        progress_cb(total, total, "")
    return [r for r in reviews if r is not None]
