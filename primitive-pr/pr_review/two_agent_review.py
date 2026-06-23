"""Two-agent Nova review orchestrator for per-file PR/commit findings.

Agent 1: whole-file review (one call per changed file, chunked).
Agent 2: dependency review (one call per changed function prompt).

Both agents run in one shared thread pool so tasks can execute concurrently.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import re
from typing import Callable, Dict, List, Tuple

from .diff import FileDiff, parse_diff
from .findings import FINDINGS_SCHEMA, Finding, dedupe, parse_findings, sort_by_severity
from .pr_passes import DEFAULT_BUDGET, pass_whole_file, read_source
from .prompt_builder import build_changed_file_chains, build_prompts_by_function

CompleteFn = Callable[[str, str], str]

DEPENDENCY_SYSTEM = (
    "You are AGENT 2 — the caller-breakage checker. Your only job is to answer one question "
    "for each DEPENDENT SYMBOL shown: does the change break this caller?\n"
    "You are given three things:\n"
    "1. The DIFF of the changed file (what changed).\n"
    "2. The CHANGED SYMBOL (the origin of the change), with line numbers in the left margin.\n"
    "3. One or more DEPENDENT SYMBOLS that call or use the changed symbol. Each is shown "
    "under a heading with its file path and line range, and every line has its real line "
    "number in the left margin.\n\n"
    "YOUR SCOPE — report only:\n"
    "- A DEPENDENT SYMBOL that still uses something the change removed, renamed, or altered "
    "(a field, return value, argument, or thrown error that no longer exists or has a different "
    "shape). Severity: critical, category: breaking.\n"
    "- Set 'file' to the DEPENDENT's file path and 'line' to the exact breaking line in that "
    "dependent — read the number from the left margin, do not count or guess.\n"
    "- Put the exact broken line of code in 'evidence'.\n"
    "- In 'explanation': name the changed symbol and what specifically changed, then name the "
    "dependent and its breaking line. Example: 'emit() now prefixes msg with [DEBUG], so "
    "Svc.run (Svc.java:3) calls logger.info() which calls emit() and will now produce "
    "unexpected output'.\n"
    "- In 'recommendation': give the concrete fix at the caller side.\n\n"
    "OUT OF SCOPE — do NOT report:\n"
    "- Bugs, style issues, or improvements inside the CHANGED SYMBOL. That is Agent 1's job.\n"
    "- Any finding in the same file as the changed symbol (Agent 1 already covers it).\n"
    "- General code quality issues in the dependent symbols unrelated to the change.\n"
    "- A dependent that does not actually call or reference what changed.\n\n"
    "GROUNDING RULES (non-negotiable):\n"
    "- Only use file paths and line numbers that literally appear in this prompt.\n"
    "- Never invent paths like `path/to/file.py`, `example.py`, or any placeholder.\n"
    "- If you cannot tie the breakage to a specific line shown here, drop the finding.\n\n"
    + FINDINGS_SCHEMA
)


@dataclass
class FileReview:
    path: str
    file_findings: List[Finding] = field(default_factory=list)
    dependency_findings: List[Finding] = field(default_factory=list)
    dependency_findings_by_function: Dict[str, List[Finding]] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


@dataclass
class PromptContext:
    prompt_id: str
    changed_file: str
    changed_function: str
    dependent_files: List[str] = field(default_factory=list)
    dependent_functions: List[Dict[str, object]] = field(default_factory=list)
    chain_paths: List[str] = field(default_factory=list)


_CHANGED_BLOCK_RE = re.compile(
    r"^### Changed [^:]+: (?P<label>.+?) \((?P<file>.+?), lines (?P<start>\d+)-(?P<end>\d+)\)$",
    re.MULTILINE,
)
_DEPENDENT_BLOCK_RE = re.compile(
    r"^### Dependent [^:]+: (?P<label>.+?) \((?P<file>.+?), lines (?P<start>\d+)-(?P<end>\d+)\)$",
    re.MULTILINE,
)


def _split_prompt_id(prompt_id: str) -> Tuple[str, str]:
    if "::" not in prompt_id:
        return prompt_id, prompt_id
    changed_file, changed_fn = prompt_id.rsplit("::", 1)
    return changed_file, changed_fn


def _context_from_prompt(
    prompt_id: str,
    prompt: str,
    chain_entry: Dict[str, object] | None,
) -> PromptContext:
    changed_file, changed_fn = _split_prompt_id(prompt_id)
    changed_m = _CHANGED_BLOCK_RE.search(prompt)
    if changed_m:
        changed_file = str(changed_m.group("file") or changed_file)
        changed_fn = str(changed_m.group("label") or changed_fn)

    dependent_functions: List[Dict[str, object]] = []
    for match in _DEPENDENT_BLOCK_RE.finditer(prompt):
        dependent_functions.append(
            {
                "label": str(match.group("label") or ""),
                "file": str(match.group("file") or ""),
                "start": int(match.group("start") or 0),
                "end": int(match.group("end") or 0),
            }
        )

    dependent_files = sorted({
        str(item.get("file") or "")
        for item in dependent_functions
        if str(item.get("file") or "").strip()
    })
    chain_paths: List[str] = []
    if chain_entry:
        dependent_files = sorted(
            set(dependent_files)
            | {
                str(p)
                for p in chain_entry.get("dependent_files", [])
                if str(p).strip()
            }
        )
        chain_paths = [
            str(p)
            for p in chain_entry.get("chain_paths", [])
            if str(p).strip()
        ]

    return PromptContext(
        prompt_id=prompt_id,
        changed_file=changed_file,
        changed_function=changed_fn,
        dependent_files=dependent_files,
        dependent_functions=dependent_functions,
        chain_paths=chain_paths,
    )


def _choose_dependent_from_finding(finding: Finding, ctx: PromptContext) -> Dict[str, object] | None:
    if not ctx.dependent_functions:
        return None
    # Strongest signal: file match + line inside dependent span.
    if finding.file and finding.line > 0:
        for dep in ctx.dependent_functions:
            dep_file = str(dep.get("file") or "")
            start = int(dep.get("start") or 0)
            end = int(dep.get("end") or 0)
            if dep_file == finding.file and start <= finding.line <= end:
                return dep
    # Next: file match only.
    if finding.file:
        same_file = [
            dep
            for dep in ctx.dependent_functions
            if str(dep.get("file") or "") == finding.file
        ]
        if len(same_file) == 1:
            return same_file[0]
    # Last: unambiguous single dependent function.
    if len(ctx.dependent_functions) == 1:
        return ctx.dependent_functions[0]
    return None


def _fallback_chain_line(ctx: PromptContext, dep: Dict[str, object] | None) -> Tuple[str, str]:
    if dep:
        dep_label = str(dep.get("label") or "").strip()
        if dep_label:
            return f"{ctx.changed_function} -> {dep_label}", "graph_function"
    if ctx.chain_paths:
        return ctx.chain_paths[0], "graph_path"
    if ctx.dependent_files:
        return f"{ctx.changed_file} -> {ctx.dependent_files[0]}", "graph_file"
    return f"{ctx.changed_function} -> (no immediate dependent found)", "graph_empty"


def _infer_rename_pair(diff_block: str) -> Tuple[str, str]:
    removed: List[str] = []
    added: List[str] = []
    for line in diff_block.splitlines():
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-"):
            removed.extend(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*[:=]", line[1:]))
        elif line.startswith("+"):
            added.extend(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*[:=]", line[1:]))

    for old in removed:
        for new in added:
            if old and new and old != new:
                return old, new
    return "", ""


def _build_mapping_text(
    changed_function: str,
    dependent_function: str,
    rename_pair: Tuple[str, str],
) -> Tuple[str, str, str]:
    old_name, new_name = rename_pair
    dep_name = dependent_function or "unknown-dependent"

    if old_name and new_name:
        impact_reason = (
            f"{changed_function} changed `{old_name}` to `{new_name}`. "
            f"Any caller like `{dep_name}` that still reads `{old_name}` can fail at runtime."
        )
        source_fix = (
            "Source fix example: keep backward compatibility in the changed API, "
            f"for example expose both `{new_name}` and `{old_name}` during migration."
        )
        dependent_fix = (
            "Dependent fix example: update dependent usage to the new API, "
            f"for example `{old_name}` -> `{new_name}` at callsites in `{dep_name}`."
        )
        return impact_reason, source_fix, dependent_fix

    impact_reason = (
        f"Changes in `{changed_function}` can impact dependent callsites in `{dep_name}`. "
        "Validate that dependent code still matches the changed behavior and contract."
    )
    source_fix = (
        "Source fix example: preserve backward-compatible behavior in the changed function "
        "or add a compatibility shim."
    )
    dependent_fix = (
        "Dependent fix example: update the dependent caller to match the new function behavior or output contract."
    )
    return impact_reason, source_fix, dependent_fix


def _enrich_dependency_findings(
    findings: List[Finding],
    ctx: PromptContext,
    rename_pair: Tuple[str, str],
    src_path: str,
) -> List[Finding]:
    allowed_files = set(ctx.dependent_files)
    if ctx.changed_file:
        allowed_files.add(ctx.changed_file)

    enriched: List[Finding] = []
    for f in findings:
        f.prompt_id = ctx.prompt_id
        f.changed_file = ctx.changed_file
        f.changed_function = ctx.changed_function

        llm_file = str(f.file or "")
        llm_line = int(f.line or 0)

        dep = _choose_dependent_from_finding(f, ctx)
        if dep:
            dep_label = str(dep.get("label") or "")
            dep_file = str(dep.get("file") or "")
            dep_start = int(dep.get("start") or 0)
            dep_end = int(dep.get("end") or 0)

            f.dependent_function = dep_label
            f.dependent_file = dep_file
            f.dependent_line = dep_start

            # For dependency findings, keep the primary file/line anchored to
            # graph-verified dependent context instead of raw LLM placeholders.
            if dep_file:
                f.file = dep_file
            if dep_start > 0 and not (llm_file == dep_file and dep_start <= llm_line <= dep_end):
                f.line = dep_start

        if _is_placeholder_path(str(f.file or "")):
            f.file = ""

        f.callsite_file = llm_file or str(f.file or "")
        f.callsite_line = llm_line or int(f.line or 0)

        # Additive only: when the change is a rename and this finding maps to a
        # dependent, append the FULL list of callsites (all places the dependent
        # reads the old attribute) to the fix text. Does not touch file/line/
        # evidence/title/explanation — only enriches the recommendation.
        old_name, _new_name = rename_pair
        if dep and old_name:
            callsites = _old_name_callsites(src_path, dep, old_name)
            if callsites:
                dep_file = str(dep.get("file") or "")
                line_list = ", ".join(str(ln) for ln, _ in callsites)
                note = f" (all callsites in {dep_file}: {line_list})"
                if note.strip() not in (f.recommendation or ""):
                    f.recommendation = (f.recommendation or "").rstrip() + note

        chain_line, chain_source = _fallback_chain_line(ctx, dep)
        f.chain_line_non_llm = chain_line
        f.chain_source = chain_source

        impact_reason, source_fix, dependent_fix = _build_mapping_text(
            ctx.changed_function,
            str(dep.get("label") or "") if dep else f.dependent_function,
            rename_pair,
        )
        if not f.impact_reason:
            f.impact_reason = (f.explanation or "").strip() or impact_reason
        if not f.source_fix_example:
            f.source_fix_example = source_fix
        if not f.dependent_fix_example:
            f.dependent_fix_example = dependent_fix

        if f.file and f.file not in allowed_files:
            f.provenance_status = "unverified_llm_claim"
        elif dep:
            f.provenance_status = "confirmed_graph_context"
        elif f.file in allowed_files:
            f.provenance_status = "graph_file_context"
        else:
            f.provenance_status = "unknown"

        enriched.append(f)
    return enriched


def _old_name_callsites(
    src_path: str,
    dep: Dict[str, object],
    old_name: str,
) -> List[Tuple[int, str]]:
    """Lines in the dependent's body that reference ``old_name`` (line_no, code)."""
    dep_file = str(dep.get("file") or "").strip()
    start = int(dep.get("start") or 0)
    end = int(dep.get("end") or 0)
    if not dep_file or not old_name or start <= 0 or end < start:
        return []
    src = read_source(src_path, dep_file, start, end)
    if not src.strip():
        return []
    # Match the renamed *attribute/field reads* — `.name`, `["name"]`, `['name']` —
    # not the bare word, so a local variable assignment like `name = ...` is NOT a hit.
    esc = re.escape(old_name)
    pattern = re.compile(rf"""(\.{esc}\b)|(\[\s*["']{esc}["']\s*\])""")
    hits: List[Tuple[int, str]] = []
    for offset, line in enumerate(src.splitlines()):
        if pattern.search(line):
            hits.append((start + offset, line.strip()))
    return hits


def _synthesize_graph_context_findings(
    findings: List[Finding],
    ctx: PromptContext,
    rename_pair: Tuple[str, str],
    src_path: str,
) -> List[Finding]:
    covered = {
        (str(f.dependent_file or ""), str(f.dependent_function or ""))
        for f in findings
        if f.provenance_status in {"confirmed_graph_context", "graph_file_context", "graph_context_only"}
    }
    old_name, _new_name = rename_pair

    synthesized: List[Finding] = list(findings)
    for dep in ctx.dependent_functions:
        dep_label = str(dep.get("label") or "").strip()
        dep_file = str(dep.get("file") or "").strip()
        dep_start = int(dep.get("start") or 0)
        key = (dep_file, dep_label)
        if key in covered:
            continue

        chain_line, chain_source = _fallback_chain_line(ctx, dep)
        impact_reason, source_fix, dependent_fix = _build_mapping_text(
            ctx.changed_function,
            dep_label,
            rename_pair,
        )

        if old_name:
            # Rename detected: only flag dependents that actually reference the old
            # name, and point at the exact line(s) where they do.
            callsites = _old_name_callsites(src_path, dep, old_name)
            if not callsites:
                continue  # dependent never reads the renamed symbol -> not a break
            first_line, first_code = callsites[0]
            line_list = ", ".join(str(ln) for ln, _ in callsites)
            precise_reason = (
                f"{ctx.changed_function} changed `{old_name}` to `{_new_name or '<new>'}`, "
                f"and `{dep_label or dep_file}` still reads `{old_name}` at "
                f"{dep_file}:{line_list}."
            )
            synthesized.append(
                Finding(
                    category="breaking",
                    severity="high",
                    file=dep_file,
                    line=first_line,
                    title=f"Dependent reads renamed `{old_name}`: {dep_label or dep_file}",
                    explanation=precise_reason,
                    evidence=first_code,
                    recommendation=dependent_fix,
                    kind="issue",
                    prompt_id=ctx.prompt_id,
                    changed_file=ctx.changed_file,
                    changed_function=ctx.changed_function,
                    dependent_function=dep_label,
                    dependent_file=dep_file,
                    dependent_line=first_line,
                    callsite_file=dep_file,
                    callsite_line=first_line,
                    chain_line_non_llm=chain_line,
                    chain_source=chain_source,
                    provenance_status="graph_context_only",
                    impact_reason=precise_reason,
                    source_fix_example=source_fix,
                    dependent_fix_example=dependent_fix,
                )
            )
            continue

        # No rename detected: emit a low-confidence deterministic impact path so the
        # dependent is still surfaced for a human to check (signature/behavior change).
        synthesized.append(
            Finding(
                category="breaking",
                severity="info",
                file=dep_file,
                line=dep_start,
                title=f"Deterministic impact path: {ctx.changed_function} -> {dep_label or dep_file}",
                explanation=impact_reason,
                evidence=chain_line,
                recommendation=f"{source_fix}\n{dependent_fix}",
                kind="issue",
                prompt_id=ctx.prompt_id,
                changed_file=ctx.changed_file,
                changed_function=ctx.changed_function,
                dependent_function=dep_label,
                dependent_file=dep_file,
                dependent_line=dep_start,
                callsite_file=dep_file,
                callsite_line=dep_start,
                chain_line_non_llm=chain_line,
                chain_source=chain_source,
                provenance_status="graph_context_only",
                impact_reason=impact_reason,
                source_fix_example=source_fix,
                dependent_fix_example=dependent_fix,
            )
        )

    return synthesized


def _is_placeholder_path(path: str) -> bool:
    p = (path or "").strip().lower().replace("\\", "/")
    if not p:
        return False
    placeholder_tokens = (
        "path/to/",
        "example.py",
        "your_file",
        "placeholder",
    )
    return any(tok in p for tok in placeholder_tokens)


def _publishable_dependency_findings(findings: List[Finding]) -> List[Finding]:
    allowed_provenance = {"confirmed_graph_context", "graph_file_context", "graph_context_only"}
    out: List[Finding] = []
    for f in findings:
        if f.provenance_status not in allowed_provenance:
            continue
        if _is_placeholder_path(str(f.file or "")):
            continue
        if _is_placeholder_path(str(f.dependent_file or "")):
            continue
        out.append(f)
    return out


def _diff_by_file(diff_text: str) -> Dict[str, str]:
    """Split unified diff into per-file diff blocks."""
    out: Dict[str, List[str]] = {}
    current_path = ""
    current_lines: List[str] = []

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current_path:
                out[current_path] = list(current_lines)
            current_path = ""
            current_lines = [line]
            continue

        current_lines.append(line)
        if line.startswith("+++ "):
            p = line[4:].strip()
            if p != "/dev/null":
                current_path = p[2:] if p.startswith("b/") else p
                out.setdefault(current_path, [])

    if current_path:
        out[current_path] = list(current_lines)

    return {k: "\n".join(v) for k, v in out.items()}


def _changed_files(file_diffs: List[FileDiff]) -> List[str]:
    return sorted({fd.path for fd in file_diffs if not fd.is_deleted and fd.added_lines})


def _chunk_text(text: str, budget: int) -> List[str]:
    if len(text) <= budget:
        return [text]
    chunks: List[str] = []
    cur: List[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > budget and cur:
            chunks.append("".join(cur))
            cur = []
            size = 0
        cur.append(line)
        size += len(line)
    if cur:
        chunks.append("".join(cur))
    return chunks


def _run_dependency_prompt(
    complete: CompleteFn,
    prompt_id: str,
    prompt: str,
    diff_block: str,
    budget: int,
) -> Tuple[str, str, List[Finding]]:
    """Run dependency agent for one changed function prompt."""
    origin_file = prompt_id.split("::", 1)[0]
    full_prompt = (
        f"# Changed file\n{origin_file}\n\n"
        f"## Diff (changed file)\n```diff\n{diff_block}\n```\n\n"
        f"{prompt}"
    )

    findings: List[Finding] = []
    chunks = _chunk_text(full_prompt, budget)
    for idx, chunk in enumerate(chunks):
        suffix = f"\n\n# Prompt part {idx + 1}/{len(chunks)}" if len(chunks) > 1 else ""
        text = complete(DEPENDENCY_SYSTEM, chunk + suffix)
        findings.extend(parse_findings(text, default_file=origin_file))

    return prompt_id, origin_file, findings


def run_two_agent_review(
    cg,
    src_path: str,
    diff_text: str,
    complete: CompleteFn,
    depth: int = 2,
    budget: int = DEFAULT_BUDGET,
    max_workers: int = 4,
) -> List[FileReview]:
    """Run two-agent review and return per-file grouped findings."""
    file_diffs = parse_diff(diff_text)
    changed_files = _changed_files(file_diffs)
    if not changed_files:
        return []

    prompts = build_prompts_by_function(cg, src_path, diff_text, depth=depth)
    chains = build_changed_file_chains(cg, diff_text, depth=depth)
    diff_map = _diff_by_file(diff_text)

    prompt_contexts: Dict[str, PromptContext] = {
        prompt_id: _context_from_prompt(prompt_id, prompt, chains.get(prompt_id))
        for prompt_id, prompt in prompts.items()
    }

    reviews: Dict[str, FileReview] = {path: FileReview(path=path) for path in changed_files}

    def _agent1(path: str) -> Tuple[str, List[Finding]]:
        return path, pass_whole_file(
            path,
            src_path,
            complete,
            budget=budget,
            diff_text=diff_map.get(path, ""),
        )

    workers = max(1, min(max_workers, len(changed_files) + len(prompts)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []

        for path in changed_files:
            futures.append(("agent1", path, pool.submit(_agent1, path)))

        for prompt_id, prompt in prompts.items():
            origin_file = prompt_id.split("::", 1)[0]
            if origin_file not in reviews:
                reviews[origin_file] = FileReview(path=origin_file)
            futures.append(
                (
                    "agent2",
                    origin_file,
                    pool.submit(
                        _run_dependency_prompt,
                        complete,
                        prompt_id,
                        prompt,
                        diff_map.get(origin_file, ""),
                        budget,
                    ),
                )
            )

        for kind, file_key, fut in futures:
            try:
                if kind == "agent1":
                    _path, findings = fut.result()
                    reviews[file_key].file_findings.extend(findings)
                else:
                    prompt_id, _origin, findings = fut.result()
                    ctx = prompt_contexts.get(prompt_id)
                    rename_pair = _infer_rename_pair(diff_map.get(file_key, ""))
                    enriched = _enrich_dependency_findings(findings, ctx, rename_pair, src_path) if ctx else findings
                    if ctx:
                        enriched = _synthesize_graph_context_findings(
                            enriched, ctx, rename_pair, src_path
                        )
                    enriched = _publishable_dependency_findings(enriched)
                    reviews[file_key].dependency_findings.extend(enriched)
                    if ctx:
                        reviews[file_key].dependency_findings_by_function.setdefault(
                            ctx.changed_function, []
                        ).extend(enriched)
            except Exception as exc:
                msg = f"{kind} failed: {exc}"
                if msg not in reviews[file_key].errors:
                    reviews[file_key].errors.append(msg)

    out: List[FileReview] = []
    for path in sorted(reviews.keys()):
        fr = reviews[path]
        fr.file_findings = sort_by_severity(dedupe(fr.file_findings))
        fr.dependency_findings = sort_by_severity(dedupe(fr.dependency_findings))
        grouped: Dict[str, List[Finding]] = {}
        for fn_name, items in fr.dependency_findings_by_function.items():
            grouped[fn_name] = sort_by_severity(dedupe(items))
        fr.dependency_findings_by_function = grouped
        out.append(fr)
    return out


def run_two_agent_review_manual(
    cg,
    src_path: str,
    selected_files: List[str],
    prompts: Dict[str, str],
    chains: Dict[str, Dict[str, object]],
    complete: CompleteFn,
    budget: int = DEFAULT_BUDGET,
    max_workers: int = 4,
) -> List[FileReview]:
    """Run two-agent review for manual file selection mode (no diff parsing)."""
    changed_files = sorted({p for p in selected_files if str(p).strip()})
    if not changed_files:
        return []

    prompt_contexts: Dict[str, PromptContext] = {
        prompt_id: _context_from_prompt(prompt_id, prompt, chains.get(prompt_id))
        for prompt_id, prompt in prompts.items()
    }

    reviews: Dict[str, FileReview] = {path: FileReview(path=path) for path in changed_files}

    def _agent1(path: str) -> Tuple[str, List[Finding]]:
        return path, pass_whole_file(
            path,
            src_path,
            complete,
            budget=budget,
            diff_text="",
        )

    workers = max(1, min(max_workers, len(changed_files) + len(prompts)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []

        for path in changed_files:
            futures.append(("agent1", path, pool.submit(_agent1, path)))

        for prompt_id, prompt in prompts.items():
            origin_file = prompt_id.split("::", 1)[0]
            if origin_file not in reviews:
                reviews[origin_file] = FileReview(path=origin_file)
            futures.append(
                (
                    "agent2",
                    origin_file,
                    pool.submit(
                        _run_dependency_prompt,
                        complete,
                        prompt_id,
                        prompt,
                        "",
                        budget,
                    ),
                )
            )

        for kind, file_key, fut in futures:
            try:
                if kind == "agent1":
                    _path, findings = fut.result()
                    reviews[file_key].file_findings.extend(findings)
                else:
                    prompt_id, _origin, findings = fut.result()
                    ctx = prompt_contexts.get(prompt_id)
                    rename_pair = ("", "")
                    enriched = _enrich_dependency_findings(findings, ctx, rename_pair, src_path) if ctx else findings
                    if ctx:
                        enriched = _synthesize_graph_context_findings(
                            enriched, ctx, rename_pair, src_path
                        )
                    enriched = _publishable_dependency_findings(enriched)
                    reviews[file_key].dependency_findings.extend(enriched)
                    if ctx:
                        reviews[file_key].dependency_findings_by_function.setdefault(
                            ctx.changed_function, []
                        ).extend(enriched)
            except Exception as exc:
                msg = f"{kind} failed: {exc}"
                if msg not in reviews[file_key].errors:
                    reviews[file_key].errors.append(msg)

    out: List[FileReview] = []
    for path in sorted(reviews.keys()):
        fr = reviews[path]
        fr.file_findings = sort_by_severity(dedupe(fr.file_findings))
        fr.dependency_findings = sort_by_severity(dedupe(fr.dependency_findings))
        grouped: Dict[str, List[Finding]] = {}
        for fn_name, items in fr.dependency_findings_by_function.items():
            grouped[fn_name] = sort_by_severity(dedupe(items))
        fr.dependency_findings_by_function = grouped
        out.append(fr)
    return out
