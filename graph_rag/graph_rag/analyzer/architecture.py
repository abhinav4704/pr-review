"""Architecture / layering check — two-stage path-shape analysis (plan.md A4).

Stage 1 (bulk, cheap, ONE batched call): walk CALLS from every
`component_role=="endpoint_handler"` root, collapsing on the *role sequence*
("shape": e.g. `controller -> service -> repository -> entity`) rather than
raw function identity — architectural smell is a property of the role
pattern, not of any one function, so hundreds of concrete chains collapse
onto a handful of shapes. Feed the LLM the whole shape catalogue (shape +
count + a few representative concrete instances) in one call; it returns
which shape-ids look architecturally/security risky. This implicitly infers
the repo's own layering convention from what's actually common — no separate
"learn the convention" call needed.

Stage 2 (targeted, one call per flagged shape): for each flagged shape's
representative instance, pull (or lazily generate) each function's
`implementation_flow` and ask a focused call to produce real findings
anchored to a specific function in that chain.

No LLM call in the graph walk itself. Same visited-set/fan-out-guard/step-
budget discipline as Stage 3's blast-radius BFS and Layer 3's taint
composition — not a new traversal system.
"""
from __future__ import annotations

from collections import defaultdict

from ..graph_core.findings import Finding
from .scoring import score_findings
from ..rag.semantic import generate_flows
from ..graph_core.store import GraphStore

# Canonical subcategory vocabulary matching findings.SEVERITY_BASE, so Stage 3's
# severity formula has a real base to key off instead of falling back to the
# flat default for free-text subcategories (found live: without this, Stage 2
# invented things like "missing_layer", "unknown_role", "backwards_layer",
# "role_repeating", "deep_chain" — none of which SEVERITY_BASE recognizes).
#
# `unclear_ownership` was added because, without a real "nothing wrong here"
# bucket separate from an actual security gap, Stage 2 was dumping vague
# observations about heuristically-unlabeled functions (e.g. "role is unclear")
# into missing_authorization just because it was the closest-fitting option —
# inflating unrelated helper functions to CRITICAL severity. Now it has an
# honest place to put "this function's role/ownership is unclear" that isn't
# a false security claim, and the system prompt also allows returning nothing.
_DESIGN_SUBCATS = [
    "layering_violation", "missing_authorization",
    "circular_architectural_dependency", "unclear_ownership",
]

MAX_DEPTH = 8
MAX_EXAMPLES_PER_SHAPE = 3
MAX_SHAPES_TO_LLM = 60
_FANOUT_CAP = 12          # children considered per node during the walk
_MAX_WALK_STEPS = 20000   # global budget across all roots, guards pathological repos

CYCLE_MARK = "<cycle>"
DEEP_MARK = "<max_depth>"


def _all_functions(store: GraphStore, repo: str) -> dict:
    rows = store.read(
        "MATCH (n:Function {repo:$repo}) RETURN n.id AS id, n.fqn AS fqn, n.file AS file, "
        "n.start_line AS start_line, n.component_role AS component_role, "
        "n.role_confidence AS role_confidence, n.docstring AS docstring",
        repo=repo,
    )
    return {r["id"]: r for r in rows}


def _adjacency(store: GraphStore, repo: str) -> dict:
    rows = store.read(
        "MATCH (a:Function {repo:$repo})-[:CALLS]->(b:Function {repo:$repo}) "
        "RETURN DISTINCT a.id AS src, b.id AS dst",
        repo=repo,
    )
    adj: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        adj[r["src"]].append(r["dst"])
    return adj


def _record(shapes: dict, roles_path: list, fqn_path: list, tail_fqn: str | None = None) -> None:
    key = tuple(roles_path)
    entry = shapes.setdefault(key, {"roles": list(roles_path), "count": 0, "examples": []})
    entry["count"] += 1
    if len(entry["examples"]) < MAX_EXAMPLES_PER_SHAPE:
        entry["examples"].append(list(fqn_path) + ([tail_fqn] if tail_fqn else []))


def collect_path_shapes(store: GraphStore, repo: str, max_depth: int = MAX_DEPTH) -> dict:
    """Returns {shape_key(tuple of roles): {"roles": [...], "count": n,
    "examples": [[fqn, ...], ...]}}. Deterministic, no LLM."""
    nodes = _all_functions(store, repo)
    adj = _adjacency(store, repo)
    roots = [nid for nid, n in nodes.items() if n.get("component_role") == "endpoint_handler"]

    shapes: dict[tuple, dict] = {}
    steps_used = 0
    # Explicit stack: (node_id, roles_path, fqn_path, visited_set)
    stack = [(r, [], [], frozenset({r})) for r in roots]

    while stack and steps_used < _MAX_WALK_STEPS:
        node_id, roles_path, fqn_path, visited = stack.pop()
        steps_used += 1
        n = nodes.get(node_id)
        if n is None:
            continue
        role = n.get("component_role") or "unknown"
        roles_path = roles_path + [role]
        fqn_path = fqn_path + [n["fqn"]]

        if len(roles_path) >= max_depth:
            _record(shapes, roles_path + [DEEP_MARK], fqn_path)
            continue

        children = [c for c in adj.get(node_id, [])[:_FANOUT_CAP] if c in nodes]
        if not children:
            _record(shapes, roles_path, fqn_path)
            continue

        for c in children:
            if c in visited:
                _record(shapes, roles_path + [CYCLE_MARK], fqn_path, tail_fqn=nodes[c]["fqn"])
                continue
            stack.append((c, roles_path, fqn_path, visited | {c}))

    return shapes


_STAGE1_SCHEMA = {
    "type": "object",
    "properties": {
        "risky_shape_ids": {
            "type": "array",
            "description": "ids (from the given catalogue) of shapes that look "
                            "architecturally or security risky",
            "items": {"type": "string"},
        }
    },
    "required": ["risky_shape_ids"],
}

_STAGE1_SYSTEM = (
    "You are reviewing a catalogue of call-chain SHAPES in a codebase, where a shape is the "
    "sequence of architectural roles a request path passes through (e.g. "
    "controller -> service -> repository -> entity), collapsed from many concrete call chains "
    "that share the same role pattern. You are given each shape's role sequence, how many "
    "concrete chains share it, and a few representative concrete function chains. "
    "Infer this repo's normal/expected layering convention from what's actually common (the "
    "highest-count shapes), then flag shape-ids that deviate from it in a way that suggests a "
    "real architecture or security problem: skipped layers (e.g. a controller reaching a "
    "repository/entity directly with no service in between), backwards calls (e.g. a repository "
    "calling back into a controller), unusually deep chains, or a `<cycle>`/`<max_depth>` marker "
    "at the end of the shape. Do not flag a shape just because it is rare if it still respects "
    "the repo's evident convention — only flag genuine deviations. "
    "IMPORTANT: role labels are produced by a heuristic classifier (name suffix, package path, "
    "or framework annotations), not verified ground truth — each role in a chain example is shown "
    "with a confidence (HIGH/MEDIUM/LOW). A `helper`/`unknown` role, or any MEDIUM/LOW-confidence "
    "role, is very often just an ordinary function the classifier couldn't confidently label (e.g. "
    "a module-level utility function) — never flag a shape as risky ONLY because it contains a "
    "helper/unclear-role step; that alone is not a layering violation. Only flag it if there is a "
    "HIGH-confidence role actually being skipped or contradicted (e.g. a HIGH-confidence controller "
    "calling a HIGH-confidence repository with no service anywhere in between)."
)


def _short_desc(row: dict) -> str:
    """First line of a function's docstring, truncated — cheap identity/intent
    context for the LLM (a graph field read, no extra LLM call), distinct from
    Stage 2's full `implementation_flow` which IS an LLM-generated summary."""
    doc = (row.get("docstring") or "").strip()
    if not doc:
        return ""
    first_line = doc.splitlines()[0].strip()
    return first_line[:100]


def _describe_chain(fqns: list[str], by_fqn: dict) -> str:
    """Render one example chain as `fqn (desc, role_confidence)` hops, so
    Stage 1 sees real function identity/intent AND how much to trust each
    role label, not just an anonymous role sequence."""
    parts = []
    for fqn in fqns:
        row = by_fqn.get(fqn, {})
        desc = _short_desc(row)
        conf = row.get("role_confidence") or "?"
        bits = [b for b in (desc, f"role_conf={conf}") if b]
        parts.append(f"{fqn} ({', '.join(bits)})" if bits else fqn)
    return " -> ".join(parts)


def flag_risky_shapes(shapes: dict, llm, by_fqn: dict | None = None, limit: int | None = None) -> set:
    """Stage 1: one batched LLM call. Returns the set of risky shape KEYS (tuples).
    Returns an empty set if there are no shapes or no llm.

    Shows the most-common shapes (real signal about the repo's convention) AND
    a long-tail sample of the rarest ones — a shape that's both rare AND risky
    would otherwise be silently truncated away by a pure count-descending cutoff.
    Each shape's example chain(s) include real function identity + a short
    docstring snippet (not just the abstract role sequence) so the LLM has
    enough context to judge intent without paying for full flow generation.
    """
    if not shapes or llm is None:
        return set()
    by_fqn = by_fqn or {}
    ranked = sorted(shapes.items(), key=lambda kv: kv[1]["count"], reverse=True)
    cap = limit or MAX_SHAPES_TO_LLM
    if len(ranked) <= cap:
        items = ranked
    else:
        tail_budget = max(1, cap // 5)   # reserve ~20% of the budget for the long tail
        head_budget = cap - tail_budget
        head = ranked[:head_budget]
        tail = ranked[-tail_budget:]
        seen_keys = {k for k, _ in head}
        items = head + [kv for kv in tail if kv[0] not in seen_keys]

    id_by_index = {}
    lines = []
    for i, (key, entry) in enumerate(items):
        sid = f"s{i}"
        id_by_index[sid] = key
        examples = entry["examples"][:2] if entry.get("examples") else []
        example_strs = [_describe_chain(ex, by_fqn) for ex in examples]
        examples_text = "; ".join(example_strs)
        lines.append(f"{sid}: roles=[{' -> '.join(entry['roles'])}] count={entry['count']} examples={examples_text}")
    user = "shape catalogue:\n" + "\n".join(lines)
    result = llm.extract(_STAGE1_SYSTEM, user, _STAGE1_SCHEMA)
    risky_ids = result.get("risky_shape_ids", []) if isinstance(result, dict) else []
    return {id_by_index[sid] for sid in risky_ids if sid in id_by_index}


_STAGE2_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subcategory": {"type": "string", "enum": _DESIGN_SUBCATS},
                    "owning_fqn": {
                        "type": "string",
                        "description": "which function in the given chain this finding is anchored to",
                    },
                    "message": {"type": "string"},
                    "evidence": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                },
                "required": ["subcategory", "owning_fqn", "message"],
            },
        }
    },
    "required": ["findings"],
}

_STAGE2_SYSTEM = (
    "You are doing a deep architectural review of ONE specific call chain, shown as an ordered "
    "list of functions with their role, role-confidence, and implementation flow. Decide whether "
    "this chain has a real architecture or security problem: a skipped/backwards layer, a chain "
    "that's too deep or convoluted to maintain, a role repeating in a way that suggests missing "
    "abstraction, or (if the chain reaches something sensitive like raw SQL/shell/filesystem) a "
    "missing validation/authorization layer given the roles shown. Anchor every finding to the "
    "single function in the chain most responsible for it, in `owning_fqn`. Do not invent behavior "
    "not shown in the given flow. "
    "IMPORTANT: role labels come from a heuristic classifier, not verified ground truth — treat "
    "MEDIUM/LOW-confidence roles and `helper`/`unknown` roles as uncertain, not as proof of a "
    "layering problem; a `helper` step with no security-sensitive action in its flow is not itself "
    "an authorization or layering issue. If a function's role is simply unclear and nothing else "
    "is wrong, use `unclear_ownership` (LOW confidence) rather than forcing it into "
    "`missing_authorization` — do not claim a missing authorization check unless the flow actually "
    "shows a sensitive operation (data mutation, deletion, raw SQL/shell/filesystem access) with no "
    "visible auth/validation step anywhere in the chain. If this chain has no real problem, return "
    "an EMPTY findings array — do not invent a finding just to say something, and do not report the "
    "same underlying issue more than once across different functions in the chain."
)


def _ensure_flows(store: GraphStore, repo: str, root: str, llm, fqns: list[str], by_fqn: dict) -> dict:
    ids = [by_fqn[f]["id"] for f in fqns if f in by_fqn]
    if llm is not None:
        generate_flows(repo, root, store, llm, ids=ids)
    rows = store.read(
        "MATCH (n:Function) WHERE n.id IN $ids RETURN n.id AS id, n.fqn AS fqn, n.file AS file, "
        "n.start_line AS start_line, n.component_role AS component_role, "
        "n.role_confidence AS role_confidence, n.implementation_flow AS implementation_flow",
        ids=ids,
    )
    return {r["fqn"]: r for r in rows}


def analyze_shape(store: GraphStore, repo: str, root: str, shape_key: tuple,
                  entry: dict, llm) -> list[Finding]:
    """Stage 2 for one flagged shape: deep-dive its representative instance."""
    if llm is None or not entry.get("examples"):
        return []
    fqns = entry["examples"][0]
    by_fqn = _all_functions_by_fqn(store, repo)
    flow_rows = _ensure_flows(store, repo, root, llm, fqns, by_fqn)

    chain_lines = []
    for fqn in fqns:
        row = flow_rows.get(fqn) or by_fqn.get(fqn, {})
        flow = row.get("implementation_flow") or []
        role = row.get("component_role") or "unknown"
        conf = row.get("role_confidence") or "?"
        chain_lines.append(
            f"- {fqn} (role={role}, role_conf={conf}):\n  " + "\n  ".join(flow or ["(no flow available)"])
        )

    user = (
        f"shape: {' -> '.join(shape_key)}\n\n"
        f"chain:\n" + "\n".join(chain_lines)
    )
    result = llm.extract(_STAGE2_SYSTEM, user, _STAGE2_SCHEMA)
    items = result.get("findings", []) if isinstance(result, dict) else []
    out = []
    for item in items:
        owning_fqn = str(item.get("owning_fqn") or fqns[0])
        row = by_fqn.get(owning_fqn, {})
        f = Finding.from_dict(item, category="design", source="llm_judged",
                              owning_fqn=owning_fqn, file=row.get("file", ""))
        if f:
            if not f.line:
                f.line = row.get("start_line") or 0
            out.append(f)
    return out


def _all_functions_by_fqn(store: GraphStore, repo: str) -> dict:
    rows = store.read(
        "MATCH (n:Function {repo:$repo}) RETURN n.id AS id, n.fqn AS fqn, n.file AS file, "
        "n.start_line AS start_line, n.component_role AS component_role, "
        "n.role_confidence AS role_confidence, n.docstring AS docstring",
        repo=repo,
    )
    return {r["fqn"]: r for r in rows}


def find_unreached_cycles(store: GraphStore, repo: str) -> list[Finding]:
    """Deterministic, no-LLM: detect call-graph cycles anywhere in the repo,
    not just ones reachable from an `endpoint_handler` root.

    `collect_path_shapes` only walks starting from route entry points, so a
    cycle entirely between internal functions that no controller/route ever
    calls into (e.g. a deliberate service<->repository call-back with no
    caller) is otherwise invisible to both Stage 1 and `deterministic_findings`
    — found live: a `notify()` <-> `log_notification()` cycle with zero
    incoming calls from any endpoint was never detected by the endpoint-rooted
    walk. One DFS pass over the WHOLE graph (white/gray/black coloring),
    O(V+E) — not per-root, so it doesn't blow up the step budget."""
    nodes = _all_functions(store, repo)
    adj = _adjacency(store, repo)
    color: dict[str, int] = {}  # 0=unvisited (absent), 1=in-progress, 2=done
    path: list[str] = []
    on_path: set[str] = set()
    seen_keys: set[tuple] = set()
    findings: list[Finding] = []
    steps = 0

    def dfs(node_id: str) -> None:
        nonlocal steps
        if steps >= _MAX_WALK_STEPS:
            return
        steps += 1
        color[node_id] = 1
        path.append(node_id)
        on_path.add(node_id)
        for child in adj.get(node_id, [])[:_FANOUT_CAP]:
            if child not in nodes or steps >= _MAX_WALK_STEPS:
                continue
            c = color.get(child, 0)
            if c == 0:
                dfs(child)
            elif c == 1 and child in on_path:
                idx = path.index(child)
                cycle_ids = path[idx:] + [child]
                key = tuple(sorted(set(cycle_ids)))
                if key not in seen_keys:
                    seen_keys.add(key)
                    example = [nodes[n]["fqn"] for n in cycle_ids]
                    anchor = nodes[cycle_ids[-2]] if len(cycle_ids) > 1 else nodes[child]
                    findings.append(Finding(
                        category="design", subcategory="circular_architectural_dependency",
                        source="graph_proven", owning_fqn=anchor["fqn"], file=anchor.get("file", ""),
                        line=anchor.get("start_line") or 0,
                        message=f"call chain cycles back through a layer boundary (no route "
                                f"reaches this chain): {' -> '.join(example)}",
                        confidence="MEDIUM",
                    ))
        path.pop()
        on_path.discard(node_id)
        color[node_id] = 2

    try:
        for nid in nodes:
            if steps >= _MAX_WALK_STEPS:
                break
            if color.get(nid, 0) == 0:
                dfs(nid)
    except RecursionError:
        pass  # best-effort on pathologically deep/wide graphs; partial results kept

    return findings


def deterministic_findings(shapes: dict, by_fqn: dict) -> list[Finding]:
    """No-LLM fallback: cycle and max-depth markers are reported directly,
    graph_proven, without any model call."""
    out: list[Finding] = []
    for key, entry in shapes.items():
        if not key:
            continue
        tail = key[-1]
        if tail not in (CYCLE_MARK, DEEP_MARK) or not entry.get("examples"):
            continue
        example = entry["examples"][0]
        anchor_fqn = example[-1] if example else ""
        row = by_fqn.get(anchor_fqn, {})
        if tail == CYCLE_MARK:
            subcat = "circular_architectural_dependency"
            msg = f"call chain cycles back through a layer boundary: {' -> '.join(example)}"
        else:
            subcat = "chain_too_deep"
            msg = f"call chain exceeds max depth without terminating: {' -> '.join(example)}"
        out.append(Finding(
            category="design", subcategory=subcat, source="graph_proven",
            owning_fqn=anchor_fqn, file=row.get("file", ""), line=row.get("start_line") or 0,
            message=msg, confidence="MEDIUM",
        ))
    return out


def run_architecture_pass(store: GraphStore, repo: str, root: str, llm=None,
                          max_depth: int = MAX_DEPTH, shape_limit: int | None = None) -> dict:
    """Full two-stage pass. With llm=None: shapes are still collected and the
    deterministic cycle/too-deep findings are still returned (dry-run safe,
    no API calls)."""
    shapes = collect_path_shapes(store, repo, max_depth=max_depth)
    by_fqn = _all_functions_by_fqn(store, repo)

    findings = deterministic_findings(shapes, by_fqn)
    seen_anchors = {(f.subcategory, f.owning_fqn) for f in findings}
    for f in find_unreached_cycles(store, repo):
        if (f.subcategory, f.owning_fqn) not in seen_anchors:
            seen_anchors.add((f.subcategory, f.owning_fqn))
            findings.append(f)
    risky_keys = flag_risky_shapes(shapes, llm, by_fqn=by_fqn, limit=shape_limit)
    for key in risky_keys:
        findings.extend(analyze_shape(store, repo, root, key, shapes[key], llm))

    if findings:
        findings = score_findings(store, repo, findings, llm=llm)

    return {
        "shapes_total": len(shapes),
        "shapes_flagged": len(risky_keys),
        "findings": findings,
    }
