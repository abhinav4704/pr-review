"""Function-summary DFG: deterministic, no LLM, no analyzer imports.

Computes per-function data-flow summaries (dfg_json) and writes PASSES edges
with flow_from_param/flow_to_param parallel arrays into the graph at index time.

Algorithm: identifier-aliasing taint tracking over one function scope (same
conservative over-approximation as taint.py's analyze_function, ported here so
it runs at index time). Key differences from the old taint.py pass:
  - Records EVERY argument, not just tainted ones (from_params/from_fields may be empty).
  - Adds field-level origin tracking (from_fields: which class fields flow in).
  - Records field writes (self.attr = param).
  - No sink classification — that stays in the analyzer at query time.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from .extractors.common import iter_descendants, text
from .languages import get_parser
from .models import Confidence, Edge, Node, Origin

# Well-known builtins that are terminal wrt taint — skip ArgFlow rows for
# them unless from_params/from_fields is non-empty (keeps dfg_json compact).
_TAINT_INERT_BUILTINS = frozenset({
    "list", "str", "dict", "set", "tuple", "sorted", "len", "int", "float",
    "bool", "repr", "print", "iter", "next", "reversed", "enumerate", "zip",
    "map", "filter", "frozenset", "bytes", "bytearray", "format", "hash",
    "min", "max", "sum", "any", "all", "abs", "round",
})

_PYTHON_LITERAL_TYPES = frozenset({
    "string", "integer", "float", "true", "false", "none",
    "concatenated_string",
})

_JAVA_LITERAL_TYPES = frozenset({
    "string_literal", "decimal_integer_literal", "decimal_floating_point_literal",
    "floating_point_literal", "true", "false", "null_literal",
    "character_literal", "octal_integer_literal", "hex_integer_literal",
    "binary_integer_literal",
})


@dataclass
class ArgFlow:
    callee: str                         # dotted-tail call name
    recv: str                           # receiver tail ("" for bare calls)
    callee_id: str = ""                 # graph node id once bound; "" = unresolved
    arg_position: Optional[int] = None  # positional index; None for keyword/splat
    arg_keyword: Optional[str] = None   # keyword name; "*"/"**" for dynamic splats
    from_params: list[int] = field(default_factory=list)
    from_fields: list[str] = field(default_factory=list)
    is_literal: bool = False
    line: int = 0

    def to_dict(self) -> dict:
        d: dict = {
            "callee": self.callee,
            "recv": self.recv,
            "line": self.line,
            "from_params": self.from_params,
            "from_fields": self.from_fields,
            "is_literal": self.is_literal,
        }
        if self.callee_id:
            d["callee_id"] = self.callee_id
        if self.arg_position is not None:
            d["arg_position"] = self.arg_position
        if self.arg_keyword is not None:
            d["arg_keyword"] = self.arg_keyword
        return d


@dataclass
class DfgSummary:
    passes: list[ArgFlow] = field(default_factory=list)
    returns_from_params: list[int] = field(default_factory=list)
    returns_from_fields: list[str] = field(default_factory=list)
    field_writes: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({
            "passes": [a.to_dict() for a in self.passes],
            "returns_from_params": self.returns_from_params,
            "returns_from_fields": self.returns_from_fields,
            "field_writes": self.field_writes,
        }, sort_keys=True)


@dataclass
class DataflowResult:
    node_props: dict[str, dict] = field(default_factory=dict)
    passes_edges: list[Edge] = field(default_factory=list)
    writes_from_params: dict[tuple, list[int]] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Python helpers (ported from taint.py, generalized)
# ---------------------------------------------------------------------------

def _callee_parts(src: bytes, fn_node) -> tuple[str, str]:
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


def _self_attr_origins(src: bytes, node, owner_class: str) -> set[str]:
    """Return field-origin strings for any self.<attr> expressions in `node`."""
    out: set[str] = set()
    candidates = [node] + list(iter_descendants(node))
    for n in candidates:
        if n.type == "attribute":
            obj = n.child_by_field_name("object")
            attr = n.child_by_field_name("attribute")
            if obj is not None and attr is not None and text(src, obj) in ("self", "cls"):
                aname = text(src, attr)
                out.add(f"{owner_class}.{aname}" if owner_class else aname)
    return out


def _own_scope(node):
    stack = list(reversed(node.children))
    while stack:
        cur = stack.pop()
        yield cur
        if cur.type not in ("function_definition", "class_definition"):
            stack.extend(reversed(cur.children))


def _call_lhs_assign_target(call_node, src: bytes) -> str:
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


def _summarize_python(
    src: bytes,
    func_node,
    param_names: list[str],
    owner_class: str,
    param_taint: dict[str, set[int]],
    field_taint: dict[str, set[str]],
    passes: list[ArgFlow],
    field_writes_out: list[dict],
    returns_from_params: list[int],
    returns_from_fields: list[str],
) -> None:
    body = func_node.child_by_field_name("body")
    if body is None:
        return

    for node in _own_scope(body):
        if node.type == "assignment":
            lhs = node.child_by_field_name("left")
            rhs = node.child_by_field_name("right")
            if lhs is None or rhs is None:
                continue

            # Gather taint sources from RHS
            rhs_params: set[int] = set()
            rhs_fields: set[str] = set()
            for rid in _identifiers(src, rhs):
                rhs_params |= param_taint.get(rid, set())
                rhs_fields |= field_taint.get(rid, set())
            rhs_fields |= _self_attr_origins(src, rhs, owner_class)

            # Check if LHS is self.<attr> — that's a field write
            if lhs.type == "attribute":
                obj = lhs.child_by_field_name("object")
                attr = lhs.child_by_field_name("attribute")
                if obj is not None and attr is not None and text(src, obj) in ("self", "cls"):
                    aname = text(src, attr)
                    if rhs_params or rhs_fields:
                        field_writes_out.append({
                            "field": f"{owner_class}.{aname}" if owner_class else aname,
                            "from_params": sorted(rhs_params),
                            "from_fields": sorted(rhs_fields),
                            "line": node.start_point[0] + 1,
                        })
                continue  # self.attr = ... doesn't affect local taint maps

            if lhs.type != "identifier":
                continue
            lname = text(src, lhs)
            if rhs_params:
                param_taint[lname] = param_taint.get(lname, set()) | rhs_params
            if rhs_fields:
                field_taint[lname] = field_taint.get(lname, set()) | rhs_fields

        elif node.type == "call":
            fn_node = node.child_by_field_name("function")
            args_node = node.child_by_field_name("arguments")
            if fn_node is None or args_node is None:
                continue
            recv, name = _callee_parts(src, fn_node)
            if not name:
                continue
            call_contributing_params: set[int] = set()
            call_contributing_fields: set[str] = set()
            line = node.start_point[0] + 1

            pos = 0
            for c in args_node.children:
                if c.type in ("(", ")", ","):
                    continue
                if c.type == "keyword_argument":
                    kw_name_node = c.child_by_field_name("name")
                    kw_val_node = c.child_by_field_name("value")
                    kw_name = text(src, kw_name_node) if kw_name_node else ""
                    p_origins: set[int] = set()
                    f_origins: set[str] = set()
                    if kw_val_node is not None:
                        for aid in _identifiers(src, kw_val_node):
                            p_origins |= param_taint.get(aid, set())
                            f_origins |= field_taint.get(aid, set())
                        f_origins |= _self_attr_origins(src, kw_val_node, owner_class)
                    is_lit = kw_val_node is not None and kw_val_node.type in _PYTHON_LITERAL_TYPES
                    if not (name in _TAINT_INERT_BUILTINS and not p_origins and not f_origins):
                        passes.append(ArgFlow(
                            callee=name, recv=recv,
                            arg_position=None, arg_keyword=kw_name,
                            from_params=sorted(p_origins), from_fields=sorted(f_origins),
                            is_literal=is_lit, line=line,
                        ))
                    call_contributing_params |= p_origins
                    call_contributing_fields |= f_origins
                    continue

                if c.type in ("list_splat", "dictionary_splat"):
                    literal_entries = _splat_literal_entries(src, c)
                    if literal_entries is not None:
                        for kind, key, val_node in literal_entries:
                            p_origins = set()
                            f_origins = set()
                            for aid in _identifiers(src, val_node):
                                p_origins |= param_taint.get(aid, set())
                                f_origins |= field_taint.get(aid, set())
                            f_origins |= _self_attr_origins(src, val_node, owner_class)
                            is_lit = val_node.type in _PYTHON_LITERAL_TYPES
                            if not (name in _TAINT_INERT_BUILTINS and not p_origins and not f_origins):
                                passes.append(ArgFlow(
                                    callee=name, recv=recv,
                                    arg_position=None,
                                    arg_keyword=key if kind == "kw" else None,
                                    from_params=sorted(p_origins), from_fields=sorted(f_origins),
                                    is_literal=is_lit, line=line,
                                ))
                            call_contributing_params |= p_origins
                            call_contributing_fields |= f_origins
                            if kind == "pos":
                                pos += 1
                        continue
                    # Dynamic splat
                    p_origins = set()
                    f_origins = set()
                    for aid in _identifiers(src, c):
                        p_origins |= param_taint.get(aid, set())
                        f_origins |= field_taint.get(aid, set())
                    marker = "**" if c.type == "dictionary_splat" else "*"
                    if not (name in _TAINT_INERT_BUILTINS and not p_origins and not f_origins):
                        passes.append(ArgFlow(
                            callee=name, recv=recv,
                            arg_position=None, arg_keyword=marker,
                            from_params=sorted(p_origins), from_fields=sorted(f_origins),
                            is_literal=False, line=line,
                        ))
                    call_contributing_params |= p_origins
                    call_contributing_fields |= f_origins
                    continue

                # plain positional argument
                p_origins = set()
                f_origins = set()
                for aid in _identifiers(src, c):
                    p_origins |= param_taint.get(aid, set())
                    f_origins |= field_taint.get(aid, set())
                f_origins |= _self_attr_origins(src, c, owner_class)
                is_lit = c.type in _PYTHON_LITERAL_TYPES
                if not (name in _TAINT_INERT_BUILTINS and not p_origins and not f_origins):
                    passes.append(ArgFlow(
                        callee=name, recv=recv,
                        arg_position=pos,
                        arg_keyword=None,
                        from_params=sorted(p_origins), from_fields=sorted(f_origins),
                        is_literal=is_lit, line=line,
                    ))
                call_contributing_params |= p_origins
                call_contributing_fields |= f_origins
                pos += 1

            # Same-function conservative return-flow propagation
            if call_contributing_params or call_contributing_fields:
                assigns_to = _call_lhs_assign_target(node, src)
                if assigns_to:
                    if call_contributing_params:
                        param_taint[assigns_to] = param_taint.get(assigns_to, set()) | call_contributing_params
                    if call_contributing_fields:
                        field_taint[assigns_to] = field_taint.get(assigns_to, set()) | call_contributing_fields

        elif node.type == "return_statement":
            for rid in _identifiers(src, node):
                returns_from_params.extend(param_taint.get(rid, set()))
                returns_from_fields.extend(field_taint.get(rid, set()))
            returns_from_fields.extend(_self_attr_origins(src, node, owner_class))


# ---------------------------------------------------------------------------
# Java helpers
# ---------------------------------------------------------------------------

_JAVA_SCOPE_STOP_TYPES = frozenset({
    "method_declaration", "constructor_declaration", "class_declaration",
    "interface_declaration", "enum_declaration", "lambda_expression",
})


def _java_identifiers(src: bytes, node) -> set[str]:
    out: set[str] = set()
    if node.type == "identifier":
        out.add(text(src, node))
    for d in iter_descendants(node):
        if d.type == "identifier":
            out.add(text(src, d))
    return out


def _java_this_field_origins(src: bytes, node, owner_class: str) -> set[str]:
    """Return field-origin strings for any this.<field> expressions in `node`."""
    out: set[str] = set()
    candidates = [node] + list(iter_descendants(node))
    for n in candidates:
        if n.type == "field_access":
            obj = n.child_by_field_name("object")
            f = n.child_by_field_name("field")
            if obj is not None and f is not None and obj.type == "this":
                fname = text(src, f)
                out.add(f"{owner_class}.{fname}" if owner_class else fname)
    return out


def _own_scope_java(node):
    stack = list(reversed(node.children))
    while stack:
        cur = stack.pop()
        yield cur
        if cur.type not in _JAVA_SCOPE_STOP_TYPES:
            stack.extend(reversed(cur.children))


def _java_callee_parts(src: bytes, invocation_node) -> tuple[str, str]:
    obj = invocation_node.child_by_field_name("object")
    name_node = invocation_node.child_by_field_name("name")
    recv = text(src, obj) if obj is not None else ""
    recv_tail = recv.rsplit(".", 1)[-1] if recv else ""
    return recv_tail, text(src, name_node) if name_node is not None else ""


def _java_call_lhs_assign_target(call_node, src: bytes) -> str:
    parent = call_node.parent
    if parent is None:
        return ""
    if parent.type == "variable_declarator":
        value = parent.child_by_field_name("value")
        name_node = parent.child_by_field_name("name")
        if value is not None and value.id == call_node.id and name_node is not None:
            return text(src, name_node)
        return ""
    if parent.type == "assignment_expression":
        right = parent.child_by_field_name("right")
        left = parent.child_by_field_name("left")
        if (right is not None and right.id == call_node.id
                and left is not None and left.type == "identifier"):
            return text(src, left)
        return ""
    return ""


def _summarize_java(
    src: bytes,
    func_node,
    param_names: list[str],
    owner_class: str,
    param_taint: dict[str, set[int]],
    field_taint: dict[str, set[str]],
    passes: list[ArgFlow],
    field_writes_out: list[dict],
    returns_from_params: list[int],
    returns_from_fields: list[str],
) -> None:
    body = func_node.child_by_field_name("body")
    if body is None:
        return

    for node in _own_scope_java(body):
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value_node = node.child_by_field_name("value")
            if name_node is None or value_node is None:
                continue
            p_origins: set[int] = set()
            f_origins: set[str] = set()
            for rid in _java_identifiers(src, value_node):
                p_origins |= param_taint.get(rid, set())
                f_origins |= field_taint.get(rid, set())
            f_origins |= _java_this_field_origins(src, value_node, owner_class)
            lname = text(src, name_node)
            if p_origins:
                param_taint[lname] = param_taint.get(lname, set()) | p_origins
            if f_origins:
                field_taint[lname] = field_taint.get(lname, set()) | f_origins

        elif node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is None or right is None:
                continue

            p_origins = set()
            f_origins = set()
            for rid in _java_identifiers(src, right):
                p_origins |= param_taint.get(rid, set())
                f_origins |= field_taint.get(rid, set())
            f_origins |= _java_this_field_origins(src, right, owner_class)

            # Check if LHS is this.<field> — field write
            if left.type == "field_access":
                obj = left.child_by_field_name("object")
                f = left.child_by_field_name("field")
                if obj is not None and f is not None and obj.type == "this":
                    fname = text(src, f)
                    if p_origins or f_origins:
                        field_writes_out.append({
                            "field": f"{owner_class}.{fname}" if owner_class else fname,
                            "from_params": sorted(p_origins),
                            "from_fields": sorted(f_origins),
                            "line": node.start_point[0] + 1,
                        })
                continue

            if left.type != "identifier":
                continue
            lname = text(src, left)
            if p_origins:
                param_taint[lname] = param_taint.get(lname, set()) | p_origins
            if f_origins:
                field_taint[lname] = field_taint.get(lname, set()) | f_origins

        elif node.type == "method_invocation":
            name_node = node.child_by_field_name("name")
            args_node = node.child_by_field_name("arguments")
            if name_node is None or args_node is None:
                continue
            recv, name = _java_callee_parts(src, node)
            if not name:
                continue
            call_contributing_params: set[int] = set()
            call_contributing_fields: set[str] = set()
            line = node.start_point[0] + 1
            pos = 0
            for c in args_node.children:
                if c.type in ("(", ")", ","):
                    continue
                p_origins = set()
                f_origins = set()
                for aid in _java_identifiers(src, c):
                    p_origins |= param_taint.get(aid, set())
                    f_origins |= field_taint.get(aid, set())
                f_origins |= _java_this_field_origins(src, c, owner_class)
                is_lit = c.type in _JAVA_LITERAL_TYPES
                if not (name in _TAINT_INERT_BUILTINS and not p_origins and not f_origins):
                    passes.append(ArgFlow(
                        callee=name, recv=recv,
                        arg_position=pos, arg_keyword=None,
                        from_params=sorted(p_origins), from_fields=sorted(f_origins),
                        is_literal=is_lit, line=line,
                    ))
                call_contributing_params |= p_origins
                call_contributing_fields |= f_origins
                pos += 1

            if call_contributing_params or call_contributing_fields:
                assigns_to = _java_call_lhs_assign_target(node, src)
                if assigns_to:
                    if call_contributing_params:
                        param_taint[assigns_to] = param_taint.get(assigns_to, set()) | call_contributing_params
                    if call_contributing_fields:
                        field_taint[assigns_to] = field_taint.get(assigns_to, set()) | call_contributing_fields

        elif node.type == "return_statement":
            for rid in _java_identifiers(src, node):
                returns_from_params.extend(param_taint.get(rid, set()))
                returns_from_fields.extend(field_taint.get(rid, set()))
            returns_from_fields.extend(_java_this_field_origins(src, node, owner_class))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def summarize_function(
    src: bytes,
    func_node,
    param_names: list[str],
    lang: str,
    owner_class: str = "",
) -> DfgSummary:
    passes: list[ArgFlow] = []
    field_writes_out: list[dict] = []
    returns_from_params: list[int] = []
    returns_from_fields: list[str] = []

    param_taint: dict[str, set[int]] = {p: {i} for i, p in enumerate(param_names) if p}
    field_taint: dict[str, set[str]] = {}

    if lang == "python":
        _summarize_python(
            src, func_node, param_names, owner_class,
            param_taint, field_taint,
            passes, field_writes_out, returns_from_params, returns_from_fields,
        )
    elif lang == "java":
        _summarize_java(
            src, func_node, param_names, owner_class,
            param_taint, field_taint,
            passes, field_writes_out, returns_from_params, returns_from_fields,
        )

    rfp = sorted(set(returns_from_params))
    rff = sorted(set(returns_from_fields))
    return DfgSummary(
        passes=passes,
        returns_from_params=rfp,
        returns_from_fields=rff,
        field_writes=field_writes_out,
    )


def _resolve_arg_position(callee_params: list[str], arg_position, arg_keyword) -> int | None:
    if arg_position is not None:
        return arg_position
    if arg_keyword in (None, "*", "**"):
        return None
    if arg_keyword in callee_params:
        return callee_params.index(arg_keyword)
    return None


def run_dataflow(
    files,
    all_nodes: list[Node],
    all_edges: list[Edge],
    repo: str,
) -> DataflowResult:
    """Compute DFG summaries for all Function nodes and build PASSES edges."""
    # Build containment: child_id -> parent_id
    parent_of: dict[str, str] = {}
    for e in all_edges:
        if e.type == "CONTAINS":
            parent_of[e.dst] = e.src
    nodes_by_id: dict[str, Node] = {n.id: n for n in all_nodes}

    def enclosing_class_name(fn_id: str) -> str:
        cur = parent_of.get(fn_id)
        while cur:
            p = nodes_by_id.get(cur)
            if p is None:
                break
            if p.label == "Class":
                return p.name or ""
            cur = parent_of.get(cur)
        return ""

    # Build function index by (file, start_line) -> Function node
    fn_by_file_line: dict[str, dict[int, Node]] = {}
    for n in all_nodes:
        if n.label == "Function" and n.file and n.start_line:
            fn_by_file_line.setdefault(n.file, {})[n.start_line] = n

    # Build CALLS edges index: caller_id -> list of (callee_name, callee_node)
    calls_by_src: dict[str, list[tuple[str, Node]]] = {}
    for e in all_edges:
        if e.type == "CALLS":
            callee = nodes_by_id.get(e.dst)
            if callee is not None:
                calls_by_src.setdefault(e.src, []).append((callee.name or "", callee))

    # Re-parse files
    _DEF_TYPES = {
        "python": frozenset({"function_definition"}),
        "java": frozenset({"method_declaration", "constructor_declaration"}),
    }

    file_map = {f.relpath: f for f in files if f.lang in ("python", "java")}

    node_props: dict[str, dict] = {}
    fn_summaries: dict[str, DfgSummary] = {}

    for relpath, f in file_map.items():
        fn_line_index = fn_by_file_line.get(relpath, {})
        if not fn_line_index:
            continue
        tree = get_parser(f.lang).parse(f.source)
        def_types = _DEF_TYPES.get(f.lang, frozenset())
        ast_by_line: dict[int, object] = {
            n.start_point[0] + 1: n
            for n in iter_descendants(tree.root_node)
            if n.type in def_types
        }
        for line, fn_node_meta in fn_line_index.items():
            ast_node = ast_by_line.get(line)
            if ast_node is None:
                continue
            owner_class = enclosing_class_name(fn_node_meta.id)
            summary = summarize_function(
                f.source, ast_node,
                fn_node_meta.param_names or [],
                f.lang, owner_class,
            )
            fn_summaries[fn_node_meta.id] = summary
            node_props[fn_node_meta.id] = {
                "dfg_json": summary.to_json(),
                "dfg_returns_from_params": summary.returns_from_params,
                "dfg_hash": fn_node_meta.body_hash,
            }

    # Bind ArgFlows to callee nodes and build PASSES edges
    # Group bound ArgFlows by (caller_id, callee_id)
    grouped: dict[tuple[str, str], list[tuple[Node, ArgFlow]]] = {}
    for fn_id, summary in fn_summaries.items():
        callee_candidates = calls_by_src.get(fn_id, [])
        callee_by_name: dict[str, list[Node]] = {}
        for cname, cnode in callee_candidates:
            callee_by_name.setdefault(cname, []).append(cnode)

        for af in summary.passes:
            candidates = callee_by_name.get(af.callee, [])
            if len(candidates) == 1:
                af.callee_id = candidates[0].id
                key = (fn_id, af.callee_id)
            else:
                key = (fn_id, "")
            caller_node = nodes_by_id.get(fn_id)
            grouped.setdefault(key, []).append((caller_node, af))

    # Build PASSES edges from grouped ArgFlows
    passes_edges: list[Edge] = []
    writes_from_params: dict[tuple, list[int]] = {}

    for (caller_id, callee_id), items in grouped.items():
        if not callee_id:
            continue  # unbound, skip
        callee_node = nodes_by_id.get(callee_id)
        if callee_node is None:
            continue
        callee_params = callee_node.param_names or []

        flow_from_param: list[int] = []
        flow_to_param: list[int] = []
        flow_lines: list[int] = []
        arg_names: list[str] = []

        # Track which callee positions get only literals
        callee_pos_literal: dict[int, bool] = {}  # pos -> all_literal so far

        for caller_node, af in items:
            fp = min(af.from_params) if af.from_params else -1
            # Map keyword to position; extractors strip self/cls so positions are already aligned
            tp = _resolve_arg_position(callee_params, af.arg_position, af.arg_keyword)
            if tp is None:
                tp = -1

            flow_from_param.append(fp)
            flow_to_param.append(tp)
            flow_lines.append(af.line)

            # arg name for display
            if af.arg_keyword and af.arg_keyword not in ("*", "**"):
                arg_names.append(af.arg_keyword)
            else:
                arg_names.append("")

            # Track literal-only positions
            if tp >= 0:
                if tp not in callee_pos_literal:
                    callee_pos_literal[tp] = af.is_literal
                else:
                    callee_pos_literal[tp] = callee_pos_literal[tp] and af.is_literal

        const_args = [pos for pos, all_lit in callee_pos_literal.items() if all_lit]

        # Find the CALLS edge confidence to copy
        calls_conf = Confidence.INFERRED.value
        for e in all_edges:
            if e.type == "CALLS" and e.src == caller_id and e.dst == callee_id:
                calls_conf = e.confidence
                break

        # First call site for evidence
        first_af = items[0][1]
        caller_node_ref = items[0][0]
        evidence_file = caller_node_ref.file if caller_node_ref else ""

        passes_edges.append(Edge(
            type="PASSES", src=caller_id, dst=callee_id,
            confidence=calls_conf,
            origin=Origin.DERIVED.value,
            extractor="dataflow",
            evidence_file=evidence_file,
            evidence_line=first_af.line,
            strategy="dfg",
            arg_names=list(arg_names),
            flow_from_param=flow_from_param,
            flow_to_param=flow_to_param,
            flow_lines=flow_lines,
            const_args=const_args,
        ))

    # WRITES enrichment: apply writes_from_params
    for fn_id, summary in fn_summaries.items():
        for fw in summary.field_writes:
            fname = fw.get("field", "")
            if fname and fw.get("from_params"):
                # key by (fn_id, field_name)
                writes_from_params[(fn_id, fname)] = fw["from_params"]

    n_bound = sum(1 for (_, cid) in grouped if cid)
    n_unbound = sum(1 for (_, cid) in grouped if not cid)

    return DataflowResult(
        node_props=node_props,
        passes_edges=passes_edges,
        writes_from_params=writes_from_params,
        stats={
            "functions": len(fn_summaries),
            "bound": n_bound,
            "unbound": n_unbound,
        },
    )
