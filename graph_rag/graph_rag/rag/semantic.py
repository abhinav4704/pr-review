"""Phase 2 — semantic enrichment (Stage 5 in ARCHITECTURE.md).

The deterministic graph holds the *facts* (callers, callees, types, reads/writes,
hierarchy, metrics). This pass adds *meaning*. It is split in two:

  Phase 2A — IDENTITY (this is the default pass, `enrich_identities`)
      A 1-2 sentence identity per node, generated bottom-up
      (Function -> Class -> Package -> Repository) so each level consumes the
      identities below it. Identity is small, cheap, and is the retrieval
      document — so we generate it for *every* node up front.

  Phase 2B — IMPLEMENTATION FLOW (lazy, `generate_flows`)
      An ordered behavioral flow for a function. Larger and only needed for
      reasoning about a function once it's actually relevant — so it is NOT run
      in bulk. Generate it on demand (for important / retrieved / changed
      functions) and cache it; never regenerate unless the body changes.

Rules followed (SEMANTIC_LAYER.md / Phase-2 spec):
  - Contract first: a fixed JSON schema the model fills, never an open summary.
  - Never restate graph facts in the output — we *feed* them as context only.
  - Progressive context: signature -> docstring -> graph facts -> raw source
    (source only when the cheaper signals are thin; the graph exists to save tokens).
  - Cache by body_hash: a node whose body hasn't changed is skipped.

Output lands as properties on the existing node (no new labels/edges):
  identity, semantic_confidence, semantic_model, semantic_provider,
  semantic_version, semantic_state, semantic_hash
  (+ implementation_flow, flow_hash once Phase 2B has run)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from ..graph_core.llm import SemanticLLM
from ..graph_core.store import GraphStore

LEVELS = ("function", "class", "package", "repository")

# Bump when the artifact schema changes, so a later run can detect and migrate v1 nodes.
# v2 adds keywords/tags/concepts retrieval signals to every identity.
SEMANTIC_VERSION = "v2"

# Caps so a god-class / huge package can't blow the prompt or cost.
_MAX_CLASS_METHODS = 40
_MAX_PACKAGE_MEMBERS = 60
_MAX_REPO_PACKAGES = 80

# semantic_state values (also a retrieval signal):
#   IDENTITY_ONLY — identity present, implementation_flow not yet generated
#   FULL          — identity + implementation_flow present
# Staleness is derived at read time: semantic_hash != current body_hash.
STATE_IDENTITY_ONLY = "IDENTITY_ONLY"
STATE_FULL = "FULL"

# --- The one generic system prompt; the schema is injected per call. ----------
SYSTEM_PROMPT = (
    "You are extracting semantic knowledge from source code for a code graph.\n"
    "Do NOT summarize syntax. The graph already contains callers, callees, types, "
    "metrics, imports, inheritance, and reads/writes — do NOT repeat those facts.\n"
    "Your task is to extract MEANING: what the entity is and why it exists.\n"
    "Identity must be concise (1-2 sentences, implementation-independent), factual, "
    "and stable. Never invent information not supported by the provided context.\n"
    "Also extract retrieval signals so this node is findable by keyword/concept "
    "search, not just vectors:\n"
    "  keywords — specific searchable terms: operations, technologies, protocols, "
    "domain verbs (e.g. Authentication, JWT, Retry, Pagination).\n"
    "  tags — coarse functional categories (e.g. security, persistence, api, "
    "validation, messaging, config).\n"
    "  concepts — domain entities/nouns this code is about (e.g. User, Order, "
    "Session, Invoice).\n"
    "Keep each list 3-8 items, deduplicated, no full sentences. Use [] if genuinely "
    "none apply.\n"
    "Return only valid JSON matching the supplied schema."
)

# Retrieval-boost arrays make identities directly findable (keyword/BM25 + concept
# filtering) alongside vector search — see SEMANTIC_LAYER.md. Stored as list
# properties on the node; embeddings are a separate later pass.
_STR_LIST = {"type": "array", "items": {"type": "string"}}
_IDENTITY_SCHEMA = {
    "type": "object",
    "properties": {
        "identity": {"type": "string"},
        "keywords": _STR_LIST,
        "tags": _STR_LIST,
        "concepts": _STR_LIST,
        "confidence": {"type": "number"},
    },
    "required": ["identity", "keywords", "tags", "concepts", "confidence"],
    "additionalProperties": False,
}
# Phase 2B — implementation flow as a typed, compressed execution graph.
# Small closed vocabulary so steps are queryable without parsing English; the
# `unknown` fallback avoids forcing the model to mislabel an odd step.
FLOW_VERSION = "v1"
FLOW_STEP_TYPES = (
    "validation", "database_read", "database_write", "business_logic",
    "external_api", "cache", "filesystem", "message_queue", "event",
    "authorization", "authentication", "response", "error", "computation",
    "unknown",
)

_FLOW_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(FLOW_STEP_TYPES)},
                    "description": {"type": "string"},
                    # candidate tokens (e.g. "c1") chosen from the list we provide;
                    # resolved back to node ids and validated after generation.
                    "references": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["type", "description", "references"],
                "additionalProperties": False,
            },
        },
        "confidence": {"type": "number"},
    },
    "required": ["steps", "confidence"],
    "additionalProperties": False,
}


@dataclass
class LevelResult:
    level: str
    total: int = 0
    generated: int = 0
    cached: int = 0
    failed: int = 0


@dataclass
class SemanticResult:
    repo: str
    levels: list[LevelResult] = field(default_factory=list)


# --- context formatting -------------------------------------------------------

def _names(values) -> str:
    seen = list(dict.fromkeys(v for v in (values or []) if v))  # dedup, keep order
    return ", ".join(seen) if seen else "—"


def _read_source(root: str, relfile: str, start: int, end: int) -> str:
    """Best-effort raw source slice (1-based inclusive lines). '' if unreadable."""
    if not (root and relfile and start):
        return ""
    try:
        with open(os.path.join(root, relfile), encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    return "".join(lines[start - 1: end or start])[:4000]  # cap so one fn can't blow the prompt


def _want_source(row: dict, source_mode: str) -> bool:
    if source_mode == "always":
        return True
    if source_mode == "never":
        return False
    return not row.get("docstring")  # "auto": only when the cheaper signals are thin


def _identity_lines(items, cap: int, rank=None) -> str:
    """Render '<name>: <identity>' lines, ranked + capped (god-class guard)."""
    items = [m for m in (items or []) if m and m.get("name")]
    if rank:
        items = sorted(items, key=rank, reverse=True)
    shown, extra = items[:cap], max(0, len(items) - cap)
    lines = "\n".join(f"  - {m['name']}: {m.get('identity') or '(no identity)'}" for m in shown)
    if extra:
        lines += f"\n  - … and {extra} more (omitted)"
    return lines or "  —"


def _method_rank(m: dict):
    vis = (m.get("visibility") or "").lower()
    public = vis not in ("private", "protected")  # treat untyped/python as public
    return (public, m.get("fan_in") or 0)


# --- Phase 2A: identity prompts (one per level) -------------------------------

def _function_identity_prompt(row: dict, root: str, source_mode: str) -> str:
    parts = [
        f"Function: {row.get('fqn') or row.get('name')}",
        f"Signature: {row.get('signature') or row.get('name')}",
    ]
    if row.get("docstring"):
        parts.append(f"Docstring: {row['docstring']}")
    parts.append("\nGraph facts (context only — do not restate in your output):")
    parts.append(f"  calls: {_names(row.get('callees'))}")
    parts.append(f"  called by: {_names(row.get('callers'))}")
    parts.append(f"  reads fields: {_names(row.get('reads'))}")
    parts.append(f"  writes fields: {_names(row.get('writes'))}")
    parts.append(f"  return type: {_names(row.get('returns'))}")
    if _want_source(row, source_mode):
        src = _read_source(root, row.get("file"), row.get("start_line") or 0, row.get("end_line") or 0)
        if src:
            parts.append("\nSource:\n" + src)
    parts.append("\nExtract a concise identity: what this function does and why it exists.")
    return "\n".join(parts)


def _class_identity_prompt(row: dict, root: str, source_mode: str) -> str:
    parts = [f"Class: {row.get('fqn') or row.get('name')}"]
    if row.get("docstring"):
        parts.append(f"Docstring: {row['docstring']}")
    if row.get("supers"):
        parts.append(f"Extends/implements (context only): {_names(row.get('supers'))}")
    parts.append("\nKey methods and their identities (public / high fan-in first):")
    parts.append(_identity_lines(row.get("methods"), _MAX_CLASS_METHODS, rank=_method_rank))
    parts.append(
        "\nExtract a single class identity describing its architectural role, "
        "responsibility, and overall purpose. Do not describe each method individually."
    )
    return "\n".join(parts)


def _package_identity_prompt(row: dict, root: str, source_mode: str) -> str:
    return (
        f"Package: {row.get('fqn') or row.get('name')}\n\n"
        "Contained classes/functions and their identities:\n"
        f"{_identity_lines(row.get('members'), _MAX_PACKAGE_MEMBERS)}\n\n"
        "Extract a single package identity describing its purpose, responsibility, "
        "and the subsystem it represents."
    )


def _repository_identity_prompt(row: dict, root: str, source_mode: str) -> str:
    return (
        f"Repository: {row.get('name')}\n\n"
        "Packages and their identities:\n"
        f"{_identity_lines(row.get('packages'), _MAX_REPO_PACKAGES)}\n\n"
        "Extract a single repository identity describing its overall purpose, major "
        "capabilities, architectural style, and primary business domain. Keep it concise."
    )


# --- per-level Cypher (gathers the node + just enough graph context) ----------

_Q_FUNCTION = """
MATCH (f:Function {repo:$repo})
OPTIONAL MATCH (f)-[:CALLS]->(callee:Function)
OPTIONAL MATCH (caller:Function)-[:CALLS]->(f)
OPTIONAL MATCH (f)-[:READS]->(rf:Field)
OPTIONAL MATCH (f)-[:WRITES]->(wf:Field)
OPTIONAL MATCH (f)-[:RETURNS]->(rt:Class)
RETURN f.id AS id, f.name AS name, f.fqn AS fqn, f.signature AS signature,
       f.docstring AS docstring, f.file AS file, f.start_line AS start_line,
       f.end_line AS end_line, f.body_hash AS body_hash, f.semantic_hash AS semantic_hash,
       f.semantic_version AS semantic_version,
       collect(DISTINCT callee.name) AS callees,
       collect(DISTINCT caller.name) AS callers,
       collect(DISTINCT rf.name) AS reads,
       collect(DISTINCT wf.name) AS writes,
       collect(DISTINCT rt.name) AS returns
"""

_Q_CLASS = """
MATCH (c:Class {repo:$repo})
OPTIONAL MATCH (c)-[:CONTAINS]->(m:Function)
OPTIONAL MATCH (c)-[:EXTENDS|IMPLEMENTS]->(sup:Class)
RETURN c.id AS id, c.name AS name, c.fqn AS fqn, c.docstring AS docstring,
       c.body_hash AS body_hash, c.semantic_hash AS semantic_hash,
       c.semantic_version AS semantic_version,
       collect(DISTINCT {name:m.name, identity:m.identity,
                         visibility:m.visibility, fan_in:m.fan_in}) AS methods,
       collect(DISTINCT sup.name) AS supers
"""

_Q_PACKAGE = """
MATCH (p:Package {repo:$repo})
OPTIONAL MATCH (p)-[:CONTAINS]->(:File)-[:CONTAINS]->(c:Class)
OPTIONAL MATCH (p)-[:CONTAINS]->(:File)-[:CONTAINS]->(fn:Function)
RETURN p.id AS id, p.name AS name, p.fqn AS fqn,
       collect(DISTINCT {name:c.name, identity:c.identity}) +
       collect(DISTINCT {name:fn.name, identity:fn.identity}) AS members
"""

_Q_REPOSITORY = """
MATCH (r:Repository {repo:$repo})
OPTIONAL MATCH (r)-[:CONTAINS*]->(p:Package)
RETURN r.id AS id, r.name AS name,
       collect(DISTINCT {name:p.name, identity:p.identity}) AS packages
"""

# level -> (cypher, prompt builder, cacheable-by-body_hash?)
_IDENTITY_SPEC = {
    "function":   (_Q_FUNCTION,   _function_identity_prompt,   True),
    "class":      (_Q_CLASS,      _class_identity_prompt,      True),
    "package":    (_Q_PACKAGE,    _package_identity_prompt,    False),  # no body; depends on children
    "repository": (_Q_REPOSITORY, _repository_identity_prompt, False),
}


def _clean_terms(values, cap: int = 12) -> list[str]:
    """Dedup (case-insensitive), trim, drop empties/sentences, cap length."""
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        s = str(v).strip()
        if not s or len(s) > 60:  # a term, not a sentence
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= cap:
            break
    return out


def _identity_props(data: dict, body_hash: str, llm: SemanticLLM) -> dict:
    return {
        "identity": (data.get("identity") or "").strip(),
        "identity_keywords": _clean_terms(data.get("keywords")),
        "identity_tags": _clean_terms(data.get("tags")),
        "identity_concepts": _clean_terms(data.get("concepts")),
        "semantic_confidence": float(data.get("confidence") or 0.0),
        "semantic_model": llm.model,
        "semantic_provider": llm.provider,
        "semantic_version": SEMANTIC_VERSION,
        "semantic_state": STATE_IDENTITY_ONLY,  # flow regenerated separately (Phase 2B)
        "semantic_hash": body_hash or "",       # cache key for incremental re-runs
    }


def enrich_identities(repo: str, root: str, store: GraphStore, llm: SemanticLLM,
                      levels=LEVELS, limit: int | None = None, refresh: bool = False,
                      source_mode: str = "auto", dry_run: bool = False,
                      log=print) -> SemanticResult:
    """Phase 2A — generate an identity for every node, bottom-up, and patch it on."""
    result = SemanticResult(repo=repo)
    for level in levels:
        query, build_prompt, cacheable = _IDENTITY_SPEC[level]
        rows = store.read(query, repo=repo)
        lr = LevelResult(level=level, total=len(rows))
        out_rows: list[dict] = []
        log(f"[{level}] {len(rows)} node(s) to check")
        for i, row in enumerate(rows, start=1):
            name = row.get("fqn") or row.get("name") or row.get("id")
            body_hash = row.get("body_hash") or ""
            fresh = (
                row.get("semantic_hash") == body_hash
                and row.get("semantic_version") == SEMANTIC_VERSION
            )
            if cacheable and not refresh and body_hash and fresh:
                lr.cached += 1
                log(f"  [{i}/{lr.total}] {level} {name} -- cached, skipped")
                continue
            if limit is not None and lr.generated >= limit:
                log(f"  [{i}/{lr.total}] {level} {name} -- limit reached, stopping level")
                break
            prompt = build_prompt(row, root, source_mode)
            if dry_run:
                lr.generated += 1
                if lr.generated == 1:
                    log(f"\n--- {level} identity dry-run ({name}) ---")
                    log(prompt)
                continue
            log(f"  [{i}/{lr.total}] {level} {name} -- generating...")
            try:
                data = llm.extract(SYSTEM_PROMPT, prompt, _IDENTITY_SCHEMA)
            except Exception as e:  # one bad node shouldn't abort the run
                lr.failed += 1
                log(f"  [{i}/{lr.total}] {level} {name} -- FAILED: {e}")
                continue
            out_rows.append({"id": row["id"], "props": _identity_props(data, body_hash, llm)})
            lr.generated += 1
            log(f"  [{i}/{lr.total}] {level} {name} -- done")
        if out_rows and not dry_run:
            store.write_semantics(out_rows)
        log(f"  {level:<11} total={lr.total:<5} generated={lr.generated:<5} "
            f"cached={lr.cached:<5} failed={lr.failed}")
        result.levels.append(lr)
    return result


# --- Phase 2B: implementation flow as a grounded, typed execution graph -------

_FLOW_SYSTEM_PROMPT = (
    "You extract the ALGORITHM of a function for a code graph — not an explanation.\n"
    "Produce an ordered sequence of meaningful execution steps. Each step answers "
    "WHAT HAPPENS (intent), not which function is called: no variable names, no "
    "syntax, no method names in the description.\n"
    "Do NOT inline helpers: a call into another function is ONE step (that callee has "
    "its own flow). Compress behavior — aim for 5-12 steps, never 40.\n"
    "Classify each step with a `type` from the schema's enum (use `unknown` only if "
    "nothing fits). For `references`, list the candidate tokens (e.g. \"c1\") of the "
    "graph nodes that step corresponds to; pick only from the provided candidates, or "
    "leave it empty.\n"
    "Return only valid JSON matching the supplied schema."
)

# Functions needing a flow: never generated (no flow_hash) or body moved since.
# Also pulls the referenceable candidate pool (outgoing edges) + read/write context.
_Q_FLOW_TARGETS = """
MATCH (f:Function {repo:$repo})
WHERE $refresh OR f.flow_hash IS NULL OR f.flow_hash <> f.body_hash
   OR f.flow_version IS NULL OR f.flow_version <> $flow_version
OPTIONAL MATCH (f)-[:CALLS]->(callee:Function)
OPTIONAL MATCH (f)-[:THROWS]->(exc:Class)
OPTIONAL MATCH (f)-[:CALLS_API]->(ep:Endpoint)
OPTIONAL MATCH (f)-[:READS]->(rf:Field)
OPTIONAL MATCH (f)-[:WRITES]->(wf:Field)
RETURN f.id AS id, f.name AS name, f.fqn AS fqn, f.signature AS signature,
       f.identity AS identity, f.docstring AS docstring, f.file AS file,
       f.start_line AS start_line, f.end_line AS end_line, f.body_hash AS body_hash,
       collect(DISTINCT {id:callee.id, kind:'Function', name:callee.fqn}) AS calls,
       collect(DISTINCT {id:exc.id, kind:'Class', name:exc.name}) AS throws,
       collect(DISTINCT {id:ep.id, kind:'Endpoint', method:ep.method, route:ep.route}) AS endpoints,
       collect(DISTINCT rf.name) AS reads,
       collect(DISTINCT wf.name) AS writes
"""


def _candidate_map(row: dict) -> dict[str, dict]:
    """token -> {id, kind, label} for every referenceable outgoing edge target."""
    out: dict[str, dict] = {}
    n = 0
    for c in row.get("calls") or []:
        if c and c.get("id"):
            n += 1
            out[f"c{n}"] = {"id": c["id"], "kind": "Function", "label": c.get("name") or "?"}
    for c in row.get("endpoints") or []:
        if c and c.get("id"):
            n += 1
            label = f"{c.get('method') or ''} {c.get('route') or ''}".strip() or "?"
            out[f"c{n}"] = {"id": c["id"], "kind": "Endpoint", "label": label}
    for c in row.get("throws") or []:
        if c and c.get("id"):
            n += 1
            out[f"c{n}"] = {"id": c["id"], "kind": "Class", "label": f"{c.get('name') or '?'} (raised)"}
    return out


def _flow_prompt(row: dict, root: str, candidates: dict[str, dict]) -> str:
    parts = [
        f"Function: {row.get('fqn') or row.get('name')}",
        f"Signature: {row.get('signature') or row.get('name')}",
    ]
    if row.get("identity"):
        parts.append(f"Identity (the goal): {row['identity']}")
    if row.get("docstring"):
        parts.append(f"Docstring: {row['docstring']}")
    parts.append("\nGraph facts (context only — do not restate):")
    parts.append(f"  reads fields: {_names(row.get('reads'))}")
    parts.append(f"  writes fields: {_names(row.get('writes'))}")
    if candidates:
        parts.append("\nReferenceable graph nodes (use these tokens in `references`):")
        for tok, c in candidates.items():
            parts.append(f"  [{tok}] {c['kind']} {c['label']}")
    else:
        parts.append("\nReferenceable graph nodes: (none — leave references empty)")
    src = _read_source(root, row.get("file"), row.get("start_line") or 0, row.get("end_line") or 0)
    if src:
        parts.append("\nSource:\n" + src)
    parts.append("\nExtract the ordered, typed steps of the algorithm.")
    return "\n".join(parts)


def _resolve_steps(data: dict, candidates: dict[str, dict]):
    """Coerce the model output to the contract and ground references against the
    candidate pool. Returns (steps, step_types, ref_ids) with hallucinated
    tokens and out-of-vocabulary types dropped/normalized."""
    steps: list[dict] = []
    for s in data.get("steps") or []:
        if not isinstance(s, dict):
            continue
        stype = s.get("type")
        if stype not in FLOW_STEP_TYPES:
            stype = "unknown"
        desc = (s.get("description") or "").strip()
        if not desc:
            continue
        refs = []
        for tok in s.get("references") or []:
            cand = candidates.get(tok)
            if cand and cand["id"] not in refs:
                refs.append(cand["id"])
        steps.append({"type": stype, "description": desc, "references": refs})
    step_types = sorted({s["type"] for s in steps})
    ref_ids = list(dict.fromkeys(r for s in steps for r in s["references"]))
    return steps, step_types, ref_ids


def generate_flows(repo: str, root: str, store: GraphStore, llm: SemanticLLM,
                   ids: list[str] | None = None, limit: int | None = None,
                   refresh: bool = False, dry_run: bool = False,
                   log=print) -> LevelResult:
    """Phase 2B — generate a typed, grounded implementation flow per function, lazily.

    Each flow is a small ordered list of typed steps where references point at real
    graph node ids (callees / thrown types / called endpoints), validated against the
    function's actual outgoing edges. Pass `ids` to target specific (e.g. retrieved /
    hot) functions; otherwise it sweeps every function still lacking a fresh flow.
    Deliberately NOT part of `enrich_identities`.
    """
    rows = store.read(_Q_FLOW_TARGETS, repo=repo, refresh=refresh, flow_version=FLOW_VERSION)
    if ids is not None:
        wanted = set(ids)
        rows = [r for r in rows if r["id"] in wanted]
    lr = LevelResult(level="flow", total=len(rows))
    out_rows: list[dict] = []
    log(f"[flow] {len(rows)} function(s) to check")
    for i, row in enumerate(rows, start=1):
        name = row.get("fqn") or row.get("name") or row.get("id")
        if limit is not None and lr.generated >= limit:
            log(f"  [{i}/{lr.total}] flow {name} -- limit reached, stopping")
            break
        candidates = _candidate_map(row)
        prompt = _flow_prompt(row, root, candidates)
        if dry_run:
            lr.generated += 1
            if lr.generated == 1:
                log(f"\n--- flow dry-run ({name}) ---")
                log(prompt)
            continue
        log(f"  [{i}/{lr.total}] flow {name} -- generating...")
        try:
            data = llm.extract(_FLOW_SYSTEM_PROMPT, prompt, _FLOW_SCHEMA)
        except Exception as e:
            lr.failed += 1
            log(f"  [{i}/{lr.total}] flow {name} -- FAILED: {e}")
            continue
        steps, step_types, ref_ids = _resolve_steps(data, candidates)
        out_rows.append({"id": row["id"], "props": {
            "implementation_flow_json": json.dumps(steps),               # full typed overlay
            "implementation_flow": [s["description"] for s in steps],     # flat quick-read list
            "flow_step_types": step_types,                               # queryable in Cypher
            "flow_references": ref_ids,                                  # queryable in Cypher
            "flow_confidence": float(data.get("confidence") or 0.0),
            "flow_model": llm.model,
            "flow_version": FLOW_VERSION,
            "flow_hash": row.get("body_hash") or "",
            "semantic_state": STATE_FULL,
        }})
        lr.generated += 1
    if out_rows and not dry_run:
        store.write_semantics(out_rows)
    log(f"  flow        total={lr.total:<5} generated={lr.generated:<5} failed={lr.failed}")
    return lr
