"""Agent B — taint composition (transfer functions) + qualify + architecture/
layering, all reading the graph/source directly. No identity or
implementation-flow generation anywhere in this module (that belongs to the
separate `rag/` tool) — per the 2-agent redesign, qualify and the
architecture deep-dive both read the RAW SOURCE of every function in the
relevant chain directly.

This module is a straight restoration + merge of the two original pre-split
modules (`taint.py` + `architecture.py`, recovered from VS Code local file
history after an accidental deletion — NOT a from-memory reconstruction),
with one deliberate change from the originals: the qualify pass (old Agent 3)
and the architecture deep-dive (old Agent 4 Stage 2) used to read each
function's `implementation_flow` (a cached LLM-generated summary from
rag/semantic.py). Per the user's explicit pivot, both now read each
function's actual raw source instead — no flow/identity generation is used
by the analyzer at all.

Python-first (v1 scope). Deliberately coarse, over-approximating value taint
at argument granularity — tracks which local variables textually derive from
which parameters (simple identifier aliasing, no CFG, no field/container
sensitivity), and where those flow into call arguments or return statements.
It will over-report; precision comes back in the qualify step, not here.

Passes, in order:
    1. `run_taint_pass`       — deterministic transfer-function + sink
                                extraction (tree-sitter, no LLM). Cached by
                                body_hash.
    2. `tag_sanitizers`       — one cached LLM call per *candidate* function
                                only (name suggests validation, or another
                                function's transfer facts actually call
                                through it). Reads raw source, not flows.
    3. `find_taint_findings`  — composition: walk transfer facts from each
                                Endpoint-handler param to a sink, no LLM.
    4. `run_taint_qualify`    — LLM confirms/denies each composed finding by
                                reading the RAW SOURCE of every function in
                                the source->sink chain.
    5. `run_architecture_pass` — Stage 1 (bulk, cheap, one batched call):
                                collapse call chains from every
                                endpoint_handler root onto role-sequence
                                "shapes"; flag risky ones. Stage 2 (targeted,
                                one call per flagged shape): read the RAW
                                SOURCE of the shape's representative chain
                                and produce anchored findings. Plus a fully
                                deterministic whole-graph cycle sweep
                                (`find_unreached_cycles`) that catches cycles
                                invisible to endpoint-rooted walks.

Known v1 limitations (name them, don't hide them):
    - Return-flow is propagated conservatively WITHIN one function only: if
      `x = foo(tainted_arg)`, `x` is treated as tainted from then on in the
      SAME function (over-approximation, not proof that `foo` actually
      returns its argument) — closes the common "A calls B, B returns it, A
      uses the return" case without a full cross-function return-taint
      solver.
    - Callee resolution for composition prefers the graph's own resolved
      CALLS edges from the caller when present, falling back to bare-name
      matching only when no resolved edge exists.
    - `*args`/`**kwargs` call-site splats are recorded but NOT precisely
      mapped to a callee parameter when dynamic (non-literal) — composition
      does not cross into the callee through them.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field

from ..graph_core.discovery import discover
from ..graph_core.extractors.common import iter_descendants, text
from ..graph_core.findings import Finding
from ..graph_core.languages import get_parser
from ..graph_core.store import GraphStore
from .scoring import score_findings

# Well-known Python builtins that are terminal/side-effect-free wrt taint
# propagation — passing a tainted value into these never itself constitutes
# an untraceable "external" continuation worth flagging. Without this, a
# call like `list(conn.execute(sql))` would spuriously flag `list` as an
# unresolved-external taint flow: the identifier scan that builds `passes`
# facts walks all identifiers nested in a call's argument expression, so it
# sees `sql` as "passed to list" even though `sql` is actually consumed by
# the inner (already-classified) `conn.execute(sql)` sink call, not by
# `list` itself. Real 3rd-party/library calls not in this list still flag.
_TAINT_INERT_BUILTINS = frozenset({
    "list", "str", "dict", "set", "tuple", "sorted", "len", "int", "float",
    "bool", "repr", "print", "iter", "next", "reversed", "enumerate", "zip",
    "map", "filter", "frozenset", "bytes", "bytearray", "format", "hash",
    "min", "max", "sum", "any", "all", "abs", "round",
})

# --- sink taxonomy (deterministic; matched against the call's dotted-tail name) ---
SINK_PATTERNS: dict[str, tuple[str, ...]] = {
    "sql_injection": ("execute", "executemany", "raw", "rawquery", "executescript"),
    "command_injection": (
        "system", "popen", "call", "run", "spawn", "getoutput",
        "check_output", "check_call", "popen2", "popen3", "popen4",
    ),
    "deserialization": ("loads", "load", "unpickle", "yaml_load", "read_pickle"),
    "path_traversal": (
        "open", "sendfile", "send_file", "remove", "unlink", "rmtree",
        "extractall", "read_text", "write_text", "readlink",
    ),
    "eval_injection": ("eval", "exec", "compile"),
    "ssrf": ("get", "post", "put", "delete", "request", "urlopen", "fetch"),
    "template_injection": ("render_template_string", "from_string"),
    "xss": ("mark_safe", "Markup"),
    "open_redirect": ("redirect", "HttpResponseRedirect"),
    # NoSQL / directory-service / XML / logging / crypto sinks — all common-word
    # names, so every one of these MUST have a receiver hint below or it will
    # false-positive constantly.
    "nosql_injection": (
        "find", "find_one", "find_one_and_update", "find_one_and_delete",
        "aggregate", "update_many", "update_one", "delete_many", "delete_one",
        "insert_many",
    ),
    "ldap_injection": ("search_s", "search", "bind_s"),
    "xxe": ("parse", "fromstring", "iterparse"),
    "sensitive_data_exposure": ("debug", "info", "warning", "warn", "error", "exception", "critical"),
    "crypto_misuse": ("encrypt", "decrypt", "new"),
}

# Some sink names above are common English words (open/get/post/find/search/...)
# — narrow them by receiver so plain local helpers/methods don't false-positive.
_SINK_RECEIVER_HINTS: dict[str, tuple[str, ...]] = {
    "path_traversal": ("os", "shutil", "", "zipfile", "tarfile", "pathlib"),
    "ssrf": ("requests", "urllib", "http", "httpx", "aiohttp", "session"),
    "nosql_injection": ("collection", "db", "mongo", "mongodb", "client", "coll"),
    "ldap_injection": ("ldap", "conn", "connection", "server"),
    "xxe": ("etree", "lxml", "xml", "sax", "minidom", "elementtree"),
    "sensitive_data_exposure": ("logger", "log", "logging"),
    "crypto_misuse": ("cipher", "aes", "des", "rsa", "crypto", "cryptography"),
}

import re as _re

_SANITIZER_NAME_HINTS = _re.compile(
    r"(sanitiz|escape|clean|validate|quote|encode|strip_tags|whitelist|allowlist)", _re.I
)


def classify_sink(recv: str, name: str) -> str | None:
    name_l = (name or "").lower()
    recv_l = (recv or "").lower()
    for vuln_class, names in SINK_PATTERNS.items():
        if name_l not in names:
            continue
        hints = _SINK_RECEIVER_HINTS.get(vuln_class)
        if hints is None or recv_l in hints:
            return vuln_class
    return None


def _callee_parts(src: bytes, fn_node) -> tuple[str, str]:
    """(receiver_tail, call_name) for a call's function expression.
    `conn.execute(...)` -> ("conn", "execute"); `eval(...)` -> ("", "eval")."""
    if fn_node.type == "attribute":
        obj = fn_node.child_by_field_name("object")
        attr = fn_node.child_by_field_name("attribute")
        recv = text(src, obj) if obj is not None else ""
        recv_tail = recv.rsplit(".", 1)[-1] if recv else ""
        return recv_tail, text(src, attr) if attr is not None else ""
    if fn_node.type == "identifier":
        return "", text(src, fn_node)
    return "", ""


def _identifiers(src: bytes, node) -> set[str]:
    out: set[str] = set()
    if node.type == "identifier":
        out.add(text(src, node))
    for d in iter_descendants(node):
        if d.type == "identifier":
            out.add(text(src, d))
    return out


def _own_scope(node):
    """Yield node's descendants, but do not descend into nested def/class bodies
    — matches python.py's own per-scope CALLS attribution philosophy."""
    stack = list(reversed(node.children))
    while stack:
        cur = stack.pop()
        yield cur
        if cur.type not in ("function_definition", "class_definition"):
            stack.extend(reversed(cur.children))


def _call_lhs_assign_target(call_node, src: bytes) -> str:
    """If `call_node` is the RHS of a plain `x = call(...)` assignment, return
    'x'; else ''. Used for same-function conservative return-flow propagation."""
    parent = call_node.parent
    if parent is None or parent.type != "assignment":
        return ""
    right = parent.child_by_field_name("right")
    left = parent.child_by_field_name("left")
    if right is None or left is None or right.id != call_node.id:
        return ""
    if left.type != "identifier":
        return ""
    return text(src, left)


def _splat_literal_entries(src: bytes, splat_node):
    """If a `*`/`**` call-site splat wraps a literal list/tuple/dict, return
    its individual (kind, key, value_node) entries so they can be mapped like
    normal positional/keyword args instead of falling back to an unmapped
    marker. kind is 'pos' or 'kw'; key is None for 'pos'. Returns None when the
    splat wraps something dynamic (a variable, another call, etc.) — those
    remain a genuine, unmappable static-analysis limitation."""
    children = [c for c in splat_node.children if c.type not in ("*", "**")]
    if not children:
        return None
    expr = children[0]
    if expr.type in ("list", "tuple"):
        entries = []
        for el in expr.children:
            if el.type in ("[", "]", "(", ")", ","):
                continue
            entries.append(("pos", None, el))
        return entries
    if expr.type == "dictionary":
        entries = []
        for pair in expr.children:
            if pair.type != "pair":
                continue
            key_node = pair.child_by_field_name("key")
            val_node = pair.child_by_field_name("value")
            if key_node is None or val_node is None:
                continue
            entries.append(("kw", text(src, key_node).strip("'\""), val_node))
        return entries
    return None


@dataclass
class FunctionTaint:
    sinks: list[dict] = field(default_factory=list)
    passes: list[dict] = field(default_factory=list)
    returns_from_params: list[int] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({
            "sinks": self.sinks,
            "passes": self.passes,
            "returns_from_params": self.returns_from_params,
        }, sort_keys=True)


def analyze_function(src: bytes, func_node, param_names: list[str]) -> FunctionTaint:
    """Compute this function's taint transfer facts. `tainted` maps a variable
    name -> the set of param indices it currently derives from; seeded with
    each param naming itself."""
    result = FunctionTaint()
    tainted: dict[str, set[int]] = {p: {i} for i, p in enumerate(param_names) if p}

    body = func_node.child_by_field_name("body")
    if body is None:
        return result

    for node in _own_scope(body):
        if node.type == "assignment":
            lhs = node.child_by_field_name("left")
            rhs = node.child_by_field_name("right")
            if lhs is None or rhs is None or lhs.type != "identifier":
                continue
            contributing: set[int] = set()
            for rid in _identifiers(src, rhs):
                contributing |= tainted.get(rid, set())
            if contributing:
                lname = text(src, lhs)
                tainted[lname] = tainted.get(lname, set()) | contributing

        elif node.type == "call":
            fn_node = node.child_by_field_name("function")
            args_node = node.child_by_field_name("arguments")
            if fn_node is None or args_node is None:
                continue
            recv, name = _callee_parts(src, fn_node)
            if not name:
                continue
            vuln_class = classify_sink(recv, name)
            call_contributing: set[int] = set()   # union across all tainted args
                                                   # (feeds same-function return propagation below)
            pos = 0
            for c in args_node.children:
                if c.type in ("(", ")", ","):
                    continue
                if c.type == "keyword_argument":
                    kw_name_node = c.child_by_field_name("name")
                    kw_val_node = c.child_by_field_name("value")
                    kw_name = text(src, kw_name_node) if kw_name_node else ""
                    contributing = set()
                    if kw_val_node is not None:
                        for aid in _identifiers(src, kw_val_node):
                            contributing |= tainted.get(aid, set())
                    if contributing:
                        call_contributing |= contributing
                        if vuln_class:
                            result.sinks.append({
                                "vuln_class": vuln_class,
                                "callee": f"{recv}.{name}" if recv else name,
                                "line": node.start_point[0] + 1,
                                "from_params": sorted(contributing),
                            })
                        else:
                            result.passes.append({
                                "callee": name,
                                "arg_position": None,
                                "arg_keyword": kw_name,
                                "from_params": sorted(contributing),
                            })
                    continue
                if c.type in ("list_splat", "dictionary_splat"):
                    literal_entries = _splat_literal_entries(src, c)
                    if literal_entries is not None:
                        # Literal `*[...]`/`**{...}` — its entries are
                        # statically known, so map each one like a normal
                        # positional/keyword arg instead of an unmapped
                        # marker.
                        for kind, key, val_node in literal_entries:
                            contributing = set()
                            for aid in _identifiers(src, val_node):
                                contributing |= tainted.get(aid, set())
                            if contributing:
                                call_contributing |= contributing
                                if vuln_class:
                                    result.sinks.append({
                                        "vuln_class": vuln_class,
                                        "callee": f"{recv}.{name}" if recv else name,
                                        "line": node.start_point[0] + 1,
                                        "from_params": sorted(contributing),
                                    })
                                elif kind == "pos":
                                    result.passes.append({
                                        "callee": name,
                                        "arg_position": None,
                                        "arg_keyword": None,
                                        "from_params": sorted(contributing),
                                    })
                                else:
                                    result.passes.append({
                                        "callee": name,
                                        "arg_position": None,
                                        "arg_keyword": key,
                                        "from_params": sorted(contributing),
                                    })
                            if kind == "pos":
                                pos += 1
                        continue
                    # Dynamic (non-literal) splat — the target parameter can't
                    # be determined syntactically, so record it as an unmapped
                    # pass (composition won't cross into the callee through
                    # it, but same-function return propagation below still
                    # applies). This remains a genuine, documented blind spot.
                    contributing = set()
                    for aid in _identifiers(src, c):
                        contributing |= tainted.get(aid, set())
                    if contributing:
                        call_contributing |= contributing
                        if vuln_class:
                            result.sinks.append({
                                "vuln_class": vuln_class,
                                "callee": f"{recv}.{name}" if recv else name,
                                "line": node.start_point[0] + 1,
                                "from_params": sorted(contributing),
                            })
                        else:
                            marker = "**" if c.type == "dictionary_splat" else "*"
                            result.passes.append({
                                "callee": name,
                                "arg_position": None,
                                "arg_keyword": marker,
                                "from_params": sorted(contributing),
                            })
                    continue
                # plain positional argument
                contributing = set()
                for aid in _identifiers(src, c):
                    contributing |= tainted.get(aid, set())
                if contributing:
                    call_contributing |= contributing
                    if vuln_class:
                        result.sinks.append({
                            "vuln_class": vuln_class,
                            "callee": f"{recv}.{name}" if recv else name,
                            "line": node.start_point[0] + 1,
                            "from_params": sorted(contributing),
                        })
                    else:
                        result.passes.append({
                            "callee": name,
                            "arg_position": pos,
                            "arg_keyword": None,
                            "from_params": sorted(contributing),
                        })
                pos += 1

            # Conservative same-function return-flow propagation: if this call's
            # result is captured by a plain `x = callee(...)` assignment and any
            # argument was tainted, treat `x` as tainted from then on in THIS
            # function too (over-approximation). Closes the common "A calls B,
            # B returns it, A uses the return in a sink" gap without a full
            # cross-function return-taint solver.
            if call_contributing:
                assigns_to = _call_lhs_assign_target(node, src)
                if assigns_to:
                    tainted[assigns_to] = tainted.get(assigns_to, set()) | call_contributing

        elif node.type == "return_statement":
            contributing = set()
            for rid in _identifiers(src, node):
                contributing |= tainted.get(rid, set())
            for i in contributing:
                if i not in result.returns_from_params:
                    result.returns_from_params.append(i)

    result.returns_from_params.sort()
    return result


def run_taint_pass(root: str, repo: str, store: GraphStore, refresh: bool = False,
                    limit: int | None = None) -> dict:
    """Extract + write transfer facts for every Python Function node in
    `repo`. Cached by body_hash — unchanged functions are skipped unless
    `refresh`."""
    rows = store.read(
        "MATCH (n:Function {repo:$repo, lang:'python'}) "
        "WHERE $refresh OR n.taint_hash IS NULL OR n.taint_hash <> n.body_hash "
        "RETURN n.id AS id, n.file AS file, n.start_line AS start_line, "
        "n.param_names AS param_names, n.body_hash AS body_hash",
        repo=repo, refresh=refresh,
    )
    if limit is not None:
        rows = rows[:limit]

    by_file: dict[str, list[dict]] = {}
    for row in rows:
        by_file.setdefault(row["file"], []).append(row)

    files = [f for f in discover(root) if f.lang == "python" and f.relpath in by_file]

    processed = 0
    sink_hits = 0
    out_rows: list[dict] = []
    for f in files:
        tree = get_parser("python").parse(f.source)
        by_line = {
            n.start_point[0] + 1: n
            for n in iter_descendants(tree.root_node)
            if n.type == "function_definition"
        }
        for row in by_file[f.relpath]:
            func_node = by_line.get(row["start_line"])
            if func_node is None:
                continue
            taint = analyze_function(f.source, func_node, row.get("param_names") or [])
            processed += 1
            sink_hits += len(taint.sinks)
            out_rows.append({
                "id": row["id"],
                "props": {
                    "taint_json": taint.to_json(),
                    "taint_hash": row.get("body_hash") or "",
                    "taint_sink_count": len(taint.sinks),
                },
            })

    if out_rows:
        store.write_semantics(out_rows)
    return {"files": len(files), "functions_processed": processed, "sink_hits": sink_hits}


# --- sanitizer tagging (one cached LLM call per candidate, reads raw source) -

_SANITIZER_SCHEMA = {
    "type": "object",
    "properties": {
        "sanitizes": {
            "type": "object",
            "description": (
                "vuln class -> list of 0-based parameter indices this function "
                "actually neutralizes for that class specifically."
            ),
            "additionalProperties": {"type": "array", "items": {"type": "integer"}},
        }
    },
    "required": ["sanitizes"],
}

_SANITIZER_SYSTEM = (
    "You are a precise static-analysis assistant. Given one function's signature, "
    "docstring, and RAW SOURCE, decide — per vulnerability class — which parameter "
    "indices it actually neutralizes as a security sanitizer for that class. "
    "A function that only lowercases, trims, or logs does NOT sanitize anything. "
    "Be conservative: if unsure, leave the class out entirely. Vulnerability "
    "classes to consider: sql_injection, command_injection, path_traversal, ssrf, "
    "template_injection, deserialization, eval_injection, xss."
)


def find_sanitizer_candidates(store: GraphStore, repo: str, refresh: bool = False) -> list[dict]:
    """Candidates worth an LLM call: name hints at sanitization, or some other
    function's transfer facts actually name this one as a callee — i.e.
    composition would really walk through it."""
    rows = store.read(
        "MATCH (n:Function {repo:$repo, lang:'python'}) "
        "RETURN n.id AS id, n.name AS name, n.fqn AS fqn, n.signature AS signature, "
        "n.docstring AS docstring, n.file AS file, n.start_line AS start_line, "
        "n.end_line AS end_line, n.body_hash AS body_hash, "
        "n.sanitizer_hash AS sanitizer_hash, n.taint_json AS taint_json",
        repo=repo,
    )
    callee_names: set[str] = set()
    for row in rows:
        tj = row.get("taint_json")
        if not tj:
            continue
        try:
            data = json.loads(tj)
        except (TypeError, ValueError):
            continue
        for p in data.get("passes", []):
            if p.get("callee"):
                callee_names.add(p["callee"])

    out = []
    for row in rows:
        name = row.get("name") or ""
        if not (name in callee_names or _SANITIZER_NAME_HINTS.search(name)):
            continue
        body_hash = row.get("body_hash") or ""
        if not refresh and body_hash and row.get("sanitizer_hash") == body_hash:
            continue
        out.append(row)
    return out


def _own_sink_params(taint_json_str: str | None) -> dict[str, set[int]]:
    """vuln_class -> set of param indices this function's OWN sinks consume.
    Used to reject a self-contradictory sanitizer tag: a function whose body
    IS the dangerous sink for (class, param) cannot also be "the sanitizer"
    for that same (class, param) — sanitization has to happen upstream of the
    sink, not be indistinguishable from it."""
    if not taint_json_str:
        return {}
    try:
        data = json.loads(taint_json_str)
    except (TypeError, ValueError):
        return {}
    out: dict[str, set[int]] = {}
    for s in data.get("sinks", []):
        out.setdefault(s["vuln_class"], set()).update(s.get("from_params", []))
    return out


def _read_source(root: str, file_rel: str, start_line: int | None, end_line: int | None) -> str:
    if not file_rel:
        return ""
    abspath = os.path.join(root, file_rel)
    try:
        with open(abspath, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    start = max((start_line or 1) - 1, 0)
    end = end_line or (start + 1)
    return "".join(lines[start:end])


def tag_sanitizers(store: GraphStore, repo: str, root: str, llm, limit: int | None = None,
                   refresh: bool = False) -> dict:
    candidates = find_sanitizer_candidates(store, repo, refresh=refresh)
    if limit is not None:
        candidates = candidates[:limit]

    root = os.path.abspath(root)
    out_rows: list[dict] = []
    tagged = 0
    for row in candidates:
        source = _read_source(root, row.get("file"), row.get("start_line"), row.get("end_line"))
        user = (
            f"function: {row.get('fqn') or row.get('name')}\n"
            f"signature: {row.get('signature') or ''}\n"
            f"docstring: {row.get('docstring') or ''}\n"
            f"source:\n{source}"
        )
        try:
            result = llm.extract(_SANITIZER_SYSTEM, user, _SANITIZER_SCHEMA)
        except Exception:
            continue  # provider/transport failure — skip, don't fail the whole pass
        sanitizes = result.get("sanitizes", {}) if isinstance(result, dict) else {}
        own_sinks = _own_sink_params(row.get("taint_json"))
        sanitizes = {
            vuln_class: kept
            for vuln_class, idxs in sanitizes.items()
            if (kept := [i for i in idxs if i not in own_sinks.get(vuln_class, set())])
        }
        out_rows.append({
            "id": row["id"],
            "props": {
                "sanitizer_json": json.dumps(sanitizes, sort_keys=True),
                "sanitizer_hash": row.get("body_hash") or "",
            },
        })
        tagged += 1

    if out_rows:
        store.write_semantics(out_rows)
    return {"candidates": len(candidates), "tagged": tagged}


# --- taint composition (deterministic, no LLM) ------------------------------

def _calls_from_map(store: GraphStore, repo: str) -> dict[str, set[str]]:
    """caller node id -> set of resolved callee node ids, from real CALLS edges
    (any confidence) — used to narrow bare-name composition candidates to the
    graph's own resolved targets when available, instead of guessing purely
    by name."""
    rows = store.read(
        "MATCH (a:Function {repo:$repo})-[:CALLS]->(b:Function) "
        "RETURN a.id AS src, b.id AS dst",
        repo=repo,
    )
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["src"], set()).add(r["dst"])
    return out


def _resolve_callees(node_row: dict, name: str, by_name: dict[str, list[dict]],
                     calls_from: dict[str, set[str]]) -> list[dict]:
    """Prefer this caller's real resolved CALLS edges over bare-name matching;
    fall back to bare-name candidates only when no resolved edge narrows it."""
    candidates = by_name.get(name, [])
    if not candidates:
        return []
    edge_targets = calls_from.get(node_row.get("id"), set())
    if edge_targets:
        narrowed = [c for c in candidates if c.get("id") in edge_targets]
        if narrowed:
            return narrowed
    return candidates


def _resolve_arg_position(callee_params: list[str], arg_position, arg_keyword) -> int | None:
    """Map a `passes` fact's arg_position/arg_keyword to the callee's actual
    0-based parameter index. Returns None if it can't be precisely mapped
    (e.g. an unmapped `*args`/`**kwargs` splat) — composition should not cross
    into the callee in that case."""
    if arg_position is not None:
        return arg_position
    if arg_keyword in (None, "*", "**"):
        return None
    if arg_keyword in callee_params:
        return callee_params.index(arg_keyword)
    return None


def find_taint_findings(store: GraphStore, repo: str, max_hops: int = 6) -> list[dict]:
    """Walk transfer facts from each Endpoint-handler parameter to a sink,
    stopping a branch early if a sanitizer covers that vuln_class on the
    incoming param. Composition runs in Python over one bulk read (arbitrary-
    depth JSON-fact composition isn't practical as a single Cypher query
    without APOC) — still fully deterministic."""
    rows = store.read(
        "MATCH (n:Function {repo:$repo}) "
        "RETURN n.id AS id, n.name AS name, n.fqn AS fqn, n.file AS file, "
        "n.start_line AS start_line, n.param_names AS param_names, "
        "n.component_role AS component_role, n.taint_json AS taint_json, "
        "n.sanitizer_json AS sanitizer_json",
        repo=repo,
    )
    by_name: dict[str, list[dict]] = {}
    for row in rows:
        by_name.setdefault(row.get("name") or "", []).append(row)
    calls_from = _calls_from_map(store, repo)

    findings: list[dict] = []
    seen: set[tuple] = set()
    for row in rows:
        if row.get("component_role") != "endpoint_handler" or not row.get("taint_json"):
            continue
        n_params = len(row.get("param_names") or [])
        for i in range(n_params):
            _walk_taint(row, i, [row.get("fqn") or row.get("name")], by_name,
                        findings, seen, max_hops, calls_from)
    return findings


def enumerate_taint_paths(
    store: GraphStore,
    repo: str,
    max_hops: int = 8,
    max_paths: int = 50000,
    include_sanitized: bool = True,
) -> list[dict]:
    """Enumerate endpoint-origin taint paths to sinks.

    Returns one record per reachable sink event with the full function chain:
    source endpoint function/param -> ... -> sink function/callee/line.
    """
    rows = store.read(
        "MATCH (n:Function {repo:$repo}) "
        "RETURN n.id AS id, n.name AS name, n.fqn AS fqn, n.file AS file, "
        "n.start_line AS start_line, n.param_names AS param_names, "
        "n.component_role AS component_role, n.taint_json AS taint_json, "
        "n.sanitizer_json AS sanitizer_json",
        repo=repo,
    )
    by_name: dict[str, list[dict]] = {}
    for row in rows:
        by_name.setdefault(row.get("name") or "", []).append(row)
    calls_from = _calls_from_map(store, repo)

    out: list[dict] = []
    seen: set[tuple] = set()
    for row in rows:
        if row.get("component_role") != "endpoint_handler" or not row.get("taint_json"):
            continue
        params = row.get("param_names") or []
        for i, pname in enumerate(params):
            _walk_taint_paths(
                node_row=row,
                param_idx=i,
                source_row=row,
                source_param_name=pname or f"param_{i}",
                chain=[row.get("fqn") or row.get("name")],
                by_name=by_name,
                out=out,
                seen=seen,
                hops_left=max_hops,
                include_sanitized=include_sanitized,
                max_paths=max_paths,
                calls_from=calls_from,
            )
            if len(out) >= max_paths:
                out.sort(
                    key=lambda p: (
                        p.get("vuln_class") or "",
                        p.get("source_fqn") or "",
                        p.get("sink_fqn") or "",
                        p.get("sink_line") or 0,
                    )
                )
                return out
    out.sort(
        key=lambda p: (
            p.get("vuln_class") or "",
            p.get("source_fqn") or "",
            p.get("sink_fqn") or "",
            p.get("sink_line") or 0,
        )
    )
    return out


def _walk_taint_paths(
    node_row: dict,
    param_idx: int,
    source_row: dict,
    source_param_name: str,
    chain: list[str],
    by_name: dict[str, list[dict]],
    out: list[dict],
    seen: set[tuple],
    hops_left: int,
    include_sanitized: bool,
    max_paths: int,
    calls_from: dict[str, set[str]],
) -> None:
    if hops_left <= 0 or len(out) >= max_paths:
        return
    taint_json = node_row.get("taint_json")
    if not taint_json:
        return
    try:
        data = json.loads(taint_json)
    except (TypeError, ValueError):
        return

    node_fqn = node_row.get("fqn") or node_row.get("name")
    source_fqn = source_row.get("fqn") or source_row.get("name")

    for sink in data.get("sinks", []):
        if param_idx not in sink.get("from_params", []):
            continue
        sanitized = _is_sanitized(node_row, sink["vuln_class"], param_idx)
        if sanitized and not include_sanitized:
            continue
        key = (
            source_row.get("id"),
            source_param_name,
            node_row.get("id"),
            param_idx,
            sink["vuln_class"],
            sink["line"],
            tuple(chain),
            bool(sanitized),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "source_fqn": source_fqn,
            "source_file": source_row.get("file"),
            "source_line": source_row.get("start_line"),
            "source_param_index": param_idx,
            "source_param_name": source_param_name,
            "vuln_class": sink["vuln_class"],
            "sink_fqn": node_fqn,
            "sink_file": node_row.get("file"),
            "sink_line": sink["line"],
            "sink_callee": sink.get("callee"),
            "path": list(chain),
            "path_hops": max(len(chain) - 1, 0),
            "sanitized": bool(sanitized),
            "status": "sanitized_on_path" if sanitized else "unsanitized_reach",
        })
        if len(out) >= max_paths:
            return

    for p in data.get("passes", []):
        if param_idx not in p.get("from_params", []):
            continue
        callee_name = p.get("callee") or ""
        callee_rows = _resolve_callees(node_row, callee_name, by_name, calls_from)
        if not callee_rows and callee_name and callee_name not in _TAINT_INERT_BUILTINS:
            # No in-repo function named `callee_name` — the graph itself is
            # the signal here (by_name has nothing for this name), so the
            # tainted value is being handed to external/stdlib/3rd-party code
            # we cannot trace further. Surface this explicitly instead of
            # silently dropping the branch, since a further sink could exist
            # inside that external call that this analysis simply can't see.
            key = (
                source_row.get("id"), source_param_name, node_row.get("id"),
                param_idx, "external_unresolved", callee_name, tuple(chain),
            )
            if key not in seen:
                seen.add(key)
                out.append({
                    "source_fqn": source_fqn,
                    "source_file": source_row.get("file"),
                    "source_line": source_row.get("start_line"),
                    "source_param_index": param_idx,
                    "source_param_name": source_param_name,
                    "vuln_class": None,
                    "sink_fqn": None,
                    "sink_file": node_row.get("file"),
                    "sink_line": None,
                    "sink_callee": callee_name,
                    "path": list(chain),
                    "path_hops": max(len(chain) - 1, 0),
                    "sanitized": False,
                    "status": "external_unresolved",
                    "external": True,
                    "confidence": "LOW",
                })
        for callee_row in callee_rows:
            callee_fqn = callee_row.get("fqn") or callee_row.get("name")
            if callee_fqn in chain:
                continue
            callee_params = callee_row.get("param_names") or []
            resolved_pos = _resolve_arg_position(callee_params, p.get("arg_position"), p.get("arg_keyword"))
            if resolved_pos is None or resolved_pos >= len(callee_params):
                continue
            _walk_taint_paths(
                node_row=callee_row,
                param_idx=resolved_pos,
                source_row=source_row,
                source_param_name=source_param_name,
                chain=chain + [callee_fqn],
                by_name=by_name,
                out=out,
                seen=seen,
                hops_left=hops_left - 1,
                include_sanitized=include_sanitized,
                max_paths=max_paths,
                calls_from=calls_from,
            )
            if len(out) >= max_paths:
                return


def _walk_taint(node_row: dict, param_idx: int, path: list[str], by_name: dict[str, list[dict]],
                 findings: list[dict], seen: set[tuple], hops_left: int,
                 calls_from: dict[str, set[str]]) -> None:
    if hops_left <= 0:
        return
    taint_json = node_row.get("taint_json")
    if not taint_json:
        return
    try:
        data = json.loads(taint_json)
    except (TypeError, ValueError):
        return

    for sink in data.get("sinks", []):
        if param_idx not in sink.get("from_params", []):
            continue
        if _is_sanitized(node_row, sink["vuln_class"], param_idx):
            continue
        key = (node_row["id"], sink["vuln_class"], sink["line"], tuple(path))
        if key in seen:
            continue
        seen.add(key)
        findings.append({
            "category": "security",
            "subcategory": sink["vuln_class"],
            "source": "graph_proven",
            "owning_fqn": node_row.get("fqn") or node_row.get("name"),
            "file": node_row.get("file"),
            "line": sink["line"],
            "message": (
                f"{sink['vuln_class']}: untrusted input reaches {sink['callee']} "
                f"via {' -> '.join(path)}"
            ),
            "path": list(path),   # full source->sink fqn chain — used by the qualify pass
        })

    for p in data.get("passes", []):
        if param_idx not in p.get("from_params", []):
            continue
        callee_name = p.get("callee") or ""
        callee_rows = _resolve_callees(node_row, callee_name, by_name, calls_from)
        if not callee_rows and callee_name and callee_name not in _TAINT_INERT_BUILTINS:
            # Graph-derived signal: `by_name` (built from all in-repo Function
            # nodes) has nothing under this name, so this is a call to
            # external/stdlib/3rd-party code — the walk truncates here, but
            # unlike a plain silent drop, we record that it was an external
            # continuation (not a dead end from lack of data) so Agent B's
            # qualify pass and Agent A's taint-flags rendering can say
            # "reaches `X` (external)" instead of the chain just trailing off.
            key = (node_row["id"], "external_unresolved", callee_name, tuple(path))
            if key not in seen:
                seen.add(key)
                findings.append({
                    "category": "security",
                    "subcategory": "unresolved_external_taint_flow",
                    "source": "graph_proven",
                    "owning_fqn": node_row.get("fqn") or node_row.get("name"),
                    "file": node_row.get("file"),
                    "line": node_row.get("start_line"),
                    "message": (
                        f"tainted value reaches external call {callee_name} "
                        f"via {' -> '.join(path)} — cannot trace further since "
                        f"{callee_name} isn't defined in this repo (may itself "
                        "reach a sink internally)."
                    ),
                    "path": list(path),
                    "external": True,
                    "confidence": "LOW",
                })
        for callee_row in callee_rows:
            callee_fqn = callee_row.get("fqn") or callee_row.get("name")
            if callee_fqn in path:
                continue  # recursion/cycle guard
            callee_params = callee_row.get("param_names") or []
            resolved_pos = _resolve_arg_position(callee_params, p.get("arg_position"), p.get("arg_keyword"))
            if resolved_pos is None or resolved_pos >= len(callee_params):
                continue
            _walk_taint(callee_row, resolved_pos, path + [callee_fqn], by_name,
                        findings, seen, hops_left - 1, calls_from)


def _is_sanitized(node_row: dict, vuln_class: str, param_idx: int) -> bool:
    sanitizer_json = node_row.get("sanitizer_json")
    if not sanitizer_json:
        return False
    try:
        san = json.loads(sanitizer_json)
    except (TypeError, ValueError):
        return False
    return param_idx in san.get(vuln_class, [])


# --- Agent B, pass 2: LLM qualify over composed taint findings --------------
#
# Reads the RAW SOURCE of every function in the source->sink chain directly
# (not a cached `implementation_flow` summary — that belongs to the separate
# rag/ tool only). Purely additive: does not touch `find_taint_findings`'s
# deterministic graph_proven output.

_QUALIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["true_positive", "false_positive", "needs_more_context"],
        },
        "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "message": {"type": "string"},
        "evidence": {"type": "string"},
        "recommendation": {"type": "string"},
    },
    "required": ["verdict", "confidence", "message"],
}

_QUALIFY_SYSTEM = (
    "You are verifying ONE proposed taint-flow security finding: untrusted input from a source "
    "parameter is claimed to reach a sink through a chain of function calls. This chain was "
    "already deterministically confirmed by static analysis (tree-sitter) to pass the tainted "
    "value into the sink with no sanitizer detected along the way — your job is to read the "
    "ACTUAL RAW SOURCE of every function in the chain (in order from source to sink) and check "
    "whether it reveals a sanitization/validation/escaping/parameterization step that the static "
    "pass might have missed, not to re-derive reachability from scratch. "
    "Default to 'true_positive' when the source code shows the value being passed straight "
    "through (string formatting, concatenation, direct parameter forwarding) with no explicit "
    "validation/sanitization/escaping/parameterization step anywhere in the chain — absence of a "
    "visible safeguard IS the signal. Only set verdict to 'false_positive' if the code explicitly "
    "sanitizes, validates, escapes, or parameterizes the value before the sink. Only use "
    "'needs_more_context' if a function's source is missing/unreadable AND that function sits "
    "between the source and the sink where a sanitizer could plausibly live. Do not invent "
    "behavior not shown in the given source."
)


def _chain_source_blocks(root: str, fqns: list[str], by_fqn: dict) -> list[str]:
    """Read the raw source of every function in `fqns`, in chain order."""
    root = os.path.abspath(root)
    lines = []
    for fqn in fqns:
        row = by_fqn.get(fqn, {})
        role = row.get("component_role") or "unknown"
        source = _read_source(root, row.get("file"), row.get("start_line"), row.get("end_line"))
        lines.append(
            f"--- {fqn} (role={role}) ---\n{source or '(source unavailable)'}"
        )
    return lines


def qualify_taint_finding(store: GraphStore, repo: str, root: str, llm,
                          finding: dict, by_fqn: dict) -> Finding | None:
    """One finding: send the chain's raw source to the LLM and return a
    `Finding` carrying its verdict, or None if the LLM says false_positive
    (nothing to report) or llm is missing."""
    if llm is None:
        return None
    fqns = finding.get("path") or [finding.get("owning_fqn")]
    chain_lines = _chain_source_blocks(root, fqns, by_fqn)

    user = (
        f"vuln_class: {finding.get('subcategory')}\n"
        f"claimed sink: {finding.get('owning_fqn')} ({finding.get('file')}:{finding.get('line')})\n\n"
        f"chain (source -> sink), raw code:\n" + "\n\n".join(chain_lines)
    )
    result = llm.extract(_QUALIFY_SYSTEM, user, _QUALIFY_SCHEMA)
    if not isinstance(result, dict):
        return None
    verdict = result.get("verdict")
    if verdict != "true_positive":
        return None   # false_positive or needs_more_context -> nothing to report here

    confidence = str(result.get("confidence", "MEDIUM")).upper()
    message = str(result.get("message") or finding.get("message") or "").strip()
    if not message:
        return None
    return Finding(
        category="security",
        subcategory=finding.get("subcategory", "unspecified"),
        source="llm_judged",
        owning_fqn=finding.get("owning_fqn", ""),
        file=finding.get("file", ""),
        line=int(finding.get("line") or 0),
        message=message,
        evidence=str(result.get("evidence", "")).strip(),
        recommendation=str(result.get("recommendation", "")).strip(),
        confidence=confidence if confidence in ("HIGH", "MEDIUM", "LOW") else "MEDIUM",
    )


def run_taint_qualify(store: GraphStore, repo: str, root: str, llm,
                      max_hops: int = 6, limit: int | None = None) -> list[Finding]:
    """Run the deterministic composition, then ask the LLM to confirm/deny
    each finding by reading the chain's raw source. Only LLM-confirmed
    true_positive findings are returned — a precision filter layered on top
    of `find_taint_findings`'s recall, not a replacement for it."""
    raw_findings = find_taint_findings(store, repo, max_hops=max_hops)
    # `external_unresolved` entries aren't confirmed sink-reaches to verify
    # sanitization on (there's no sink to check) — they're a coverage-gap
    # signal, already surfaced separately at LOW confidence by the caller
    # (cli.py's stage1) directly from `find_taint_findings`'s raw output.
    raw_findings = [f for f in raw_findings if not f.get("external")]
    if limit is not None:
        raw_findings = raw_findings[:limit]
    by_fqn = {r["fqn"]: r for r in store.read(
        "MATCH (n:Function {repo:$repo}) RETURN n.id AS id, n.fqn AS fqn, n.file AS file, "
        "n.start_line AS start_line, n.end_line AS end_line, "
        "n.component_role AS component_role, n.role_confidence AS role_confidence",
        repo=repo,
    ) if r.get("fqn")}

    out: list[Finding] = []
    for finding in raw_findings:
        f = qualify_taint_finding(store, repo, root, llm, finding, by_fqn)
        if f is not None:
            out.append(f)
    return out


# --- Agent B: architecture / layering — two-stage path-shape analysis ------
#
# Stage 1 (bulk, cheap, ONE batched call): walk CALLS from every
# `component_role=="endpoint_handler"` root, collapsing on the *role
# sequence* ("shape": e.g. `controller -> service -> repository -> entity`)
# rather than raw function identity — architectural smell is a property of
# the role pattern, not of any one function, so hundreds of concrete chains
# collapse onto a handful of shapes. Feed the LLM the whole shape catalogue
# (shape + count + a few representative concrete instances, each with a
# short docstring snippet — cheap graph reads, no extra LLM calls) in one
# call; it returns which shape-ids look architecturally/security risky.
#
# Stage 2 (targeted, one call per flagged shape): for each flagged shape's
# representative instance, read the RAW SOURCE of every function in that
# chain directly and ask a focused call to produce real findings anchored to
# a specific function in that chain.
#
# Plus a fully deterministic whole-graph cycle sweep (`find_unreached_cycles`)
# that catches cycles invisible to endpoint-rooted walks (no controller/route
# ever calls into them).

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
        "n.start_line AS start_line, n.end_line AS end_line, n.component_role AS component_role, "
        "n.role_confidence AS role_confidence, n.docstring AS docstring",
        repo=repo,
    )
    return {r["id"]: r for r in rows}


def _all_functions_by_fqn(store: GraphStore, repo: str) -> dict:
    rows = store.read(
        "MATCH (n:Function {repo:$repo}) RETURN n.id AS id, n.fqn AS fqn, n.file AS file, "
        "n.start_line AS start_line, n.end_line AS end_line, n.component_role AS component_role, "
        "n.role_confidence AS role_confidence, n.docstring AS docstring",
        repo=repo,
    )
    return {r["fqn"]: r for r in rows}


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
    context for the LLM (a graph field read, no extra LLM call). Stage 2's
    deep-dive reads full raw source instead, only for flagged shapes."""
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
    """Stage 1: one batched LLM call. Returns the set of risky shape KEYS
    (tuples). Returns an empty set if there are no shapes or no llm.

    Shows the most-common shapes (real signal about the repo's convention) AND
    a long-tail sample of the rarest ones — a shape that's both rare AND risky
    would otherwise be silently truncated away by a pure count-descending
    cutoff.
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
    "You are doing a deep architectural review of ONE specific call chain. You are given the RAW "
    "SOURCE of every function in the chain, in order, each labeled with its role and role-"
    "confidence. Decide whether this chain has a real architecture or security problem: a skipped/"
    "backwards layer, a chain that's too deep or convoluted to maintain, a role repeating in a way "
    "that suggests missing abstraction, or (if the chain reaches something sensitive like raw SQL/"
    "shell/filesystem) a missing validation/authorization layer given the roles shown. Anchor every "
    "finding to the single function in the chain most responsible for it, in `owning_fqn`. Do not "
    "invent behavior not shown in the given source. "
    "IMPORTANT: role labels come from a heuristic classifier, not verified ground truth — treat "
    "MEDIUM/LOW-confidence roles and `helper`/`unknown` roles as uncertain, not as proof of a "
    "layering problem; a `helper` step with no security-sensitive action in its source is not itself "
    "an authorization or layering issue. If a function's role is simply unclear and nothing else "
    "is wrong, use `unclear_ownership` (LOW confidence) rather than forcing it into "
    "`missing_authorization` — do not claim a missing authorization check unless the code actually "
    "shows a sensitive operation (data mutation, deletion, raw SQL/shell/filesystem access) with no "
    "visible auth/validation step anywhere in the chain. If this chain has no real problem, return "
    "an EMPTY findings array — do not invent a finding just to say something, and do not report the "
    "same underlying issue more than once across different functions in the chain."
)


def _analyze_shape_instance(store: GraphStore, by_fqn: dict, root: str, shape_key: tuple,
                            fqns: list, llm) -> list[Finding]:
    """Deep-dive ONE concrete chain instance of a flagged shape by reading the
    raw source of every function in it. Split out of `analyze_shape` so every
    example instance of a shape gets its own LLM call, not just the first."""
    chain_lines = []
    for fqn, src_block in zip(fqns, _chain_source_blocks(root, fqns, by_fqn)):
        row = by_fqn.get(fqn, {})
        role = row.get("component_role") or "unknown"
        conf = row.get("role_confidence") or "?"
        chain_lines.append(f"{src_block}\n(role={role}, role_conf={conf})")

    user = (
        f"shape: {' -> '.join(shape_key)}\n\n"
        f"chain (source -> sink), raw code:\n" + "\n\n".join(chain_lines)
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


def analyze_shape(store: GraphStore, repo: str, root: str, shape_key: tuple,
                  entry: dict, llm) -> list[Finding]:
    """Stage 2 for one flagged shape: deep-dive EVERY stored representative
    instance (up to MAX_EXAMPLES_PER_SHAPE), not just the first — a shape
    with 2+ examples (e.g. two different endpoints both skipping the service
    layer) previously only ever had its first example analyzed, silently
    dropping real findings anchored on the other instance(s). Findings are
    deduped by (subcategory, owning_fqn, chain_root) — chain_root (the
    example's own entry point) is included so two DIFFERENT endpoints that
    happen to share a tail function (e.g. both call the same repository
    method directly) each still get their own finding instead of the second
    endpoint's finding being silently swallowed just because it anchored on
    the same shared owning_fqn as the first endpoint's chain."""
    if llm is None or not entry.get("examples"):
        return []
    by_fqn = _all_functions_by_fqn(store, repo)
    out: list[Finding] = []
    seen: set[tuple] = set()
    for fqns in entry["examples"]:
        chain_root = fqns[0] if fqns else ""
        for f in _analyze_shape_instance(store, by_fqn, root, shape_key, fqns, llm):
            key = (f.subcategory, f.owning_fqn, chain_root)
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
    return out


def find_unreached_cycles(store: GraphStore, repo: str) -> list[Finding]:
    """Deterministic, no-LLM: detect call-graph cycles anywhere in the repo,
    not just ones reachable from an `endpoint_handler` root.

    `collect_path_shapes` only walks starting from route entry points, so a
    cycle entirely between internal functions that no controller/route ever
    calls into is otherwise invisible to both Stage 1 and
    `deterministic_findings`. One DFS pass over the WHOLE graph (white/gray/
    black coloring), O(V+E) — not per-root, so it doesn't blow up the step
    budget."""
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
    no API calls). Severity scoring is left to the caller (cli.py/frontend
    score everything together after merging Agent A + Agent B findings), not
    done inline here, to avoid double-scoring the same findings."""
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

    return {
        "shapes_total": len(shapes),
        "shapes_flagged": len(risky_keys),
        "findings": findings,
    }


# --- Agent B orchestrator ----------------------------------------------------

def run_agent_b(store: GraphStore, repo: str, root: str, llm,
                max_hops: int = 6, qualify_limit: int | None = None,
                shape_limit: int | None = None, tag_limit: int | None = None) -> dict:
    """Single entry point running all of Agent B's passes in order: taint
    tagging, deterministic composition, LLM qualify (raw-source chains), and
    architecture/layering (shape catalogue + raw-source deep-dive + whole-
    graph cycle sweep)."""
    tag_stats = run_taint_pass(root, repo, store, limit=tag_limit)
    det_findings = find_taint_findings(store, repo, max_hops=max_hops)
    qualified = run_taint_qualify(store, repo, root, llm, max_hops=max_hops, limit=qualify_limit) if llm else []
    arch = run_architecture_pass(store, repo, root, llm, shape_limit=shape_limit)
    return {
        "tag_stats": tag_stats,
        "det_findings": det_findings,
        "qualified": qualified,
        "arch_findings": arch["findings"],
        "shapes_total": arch["shapes_total"],
        "shapes_flagged": arch["shapes_flagged"],
    }
