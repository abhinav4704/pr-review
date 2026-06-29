"""Stage 1 — Python structural extraction via tree-sitter.

Emits:
    Nodes: File, Class, Function(kind=method|function), Field (class-level)
           — with full metadata (range incl. columns, visibility, modifiers,
             is_async/static/abstract, return_type, param_count, docstring).
    Edges (resolved): CONTAINS — with provenance (origin/extractor/evidence loc).
        RawRefs (name-only): IMPORTS, EXTENDS, CALLS, ANNOTATED_WITH (decorators)
           — each carrying the reference-site location for edge provenance.
"""
from __future__ import annotations

import os

from ..apispec import (
    HTTP_CLIENT_RECEIVERS,
    HTTP_METHODS,
    endpoint_display,
    endpoint_fqn,
    endpoint_id,
    normalize_route,
    split_url,
)
from ..discovery import FileInfo
from ..ids import body_hash, make_id
from ..models import Edge, Node, Origin, RawRef
from ..languages import get_parser
from .common import text

EXTRACTOR = "tree-sitter"


def _module_fqn(relpath: str) -> str:
    no_ext = os.path.splitext(relpath)[0]
    parts = [p for p in no_ext.split(os.sep) if p and p != "__init__"]
    return ".".join(parts)


def _visibility(name: str) -> str:
    """Python has no keywords; use the leading-underscore convention."""
    if name.startswith("__") and not name.endswith("__"):
        return "private"
    if name.startswith("_") and not name.endswith("__"):
        return "protected"
    return "public"


def extract(file: FileInfo, repo: str):
    src = file.source
    tree = get_parser("python").parse(src)
    root = tree.root_node

    nodes: list[Node] = []
    edges: list[Edge] = []
    refs: list[RawRef] = []
    created_fields: set[str] = set()   # field ids already materialized (dedup)

    def contains(container_id: str, child_node, child_id: str):
        edges.append(Edge(
            "CONTAINS", container_id, child_id,
            origin=Origin.EXTRACTED.value, extractor=EXTRACTOR,
            evidence_file=file.relpath,
            evidence_line=child_node.start_point[0] + 1,
            evidence_col=child_node.start_point[1],
        ))

    def ref(
        rtype,
        src_id,
        target,
        kind_hint,
        node,
        recv="",
        call_arity=-1,
        recv_type="",
        import_fqn="",
    ):
        refs.append(RawRef(
            rtype, src_id, target, kind_hint, recv=recv,
            recv_type=recv_type,
            import_fqn=import_fqn,
            ref_file=file.relpath,
            ref_line=node.start_point[0] + 1, ref_col=node.start_point[1],
            call_arity=call_arity,
        ))

    def emit_endpoint(method, route, handler_id, ev_node):
        """An in-repo route definition: an Endpoint node + EXPOSES from its handler."""
        eid = endpoint_id(repo, method, route)
        nodes.append(Node(
            id=eid, label="Endpoint", name=endpoint_display(method, route),
            fqn=endpoint_fqn(method, route), repo=repo, kind="endpoint",
            lang="python", file=file.relpath,
            start_line=ev_node.start_point[0] + 1, start_col=ev_node.start_point[1],
            end_line=ev_node.end_point[0] + 1, end_col=ev_node.end_point[1],
            method=method.upper(), route=normalize_route(route), extractor=EXTRACTOR,
        ))
        edges.append(Edge(
            "EXPOSES", handler_id, eid,
            origin=Origin.EXTRACTED.value, extractor=EXTRACTOR,
            evidence_file=file.relpath,
            evidence_line=ev_node.start_point[0] + 1, evidence_col=ev_node.start_point[1],
        ))

    def emit_api_call(handler_id, method, url, ev_node):
        """An outbound HTTP call: a CALLS_API ref resolved against the endpoint index."""
        host, path = split_url(url)
        refs.append(RawRef(
            "CALLS_API", handler_id, path, "api", recv=host, http_method=method,
            ref_file=file.relpath,
            ref_line=ev_node.start_point[0] + 1, ref_col=ev_node.start_point[1],
        ))

    module_fqn = _module_fqn(file.relpath)
    file_id = make_id(repo, file.relpath, "file")
    file_package = os.path.dirname(file.relpath).replace(os.sep, ".")
    nodes.append(Node(
        id=file_id, label="File", name=os.path.basename(file.relpath),
        fqn=file.relpath, repo=repo, kind="file", lang="python",
        file=file.relpath, package=file_package, start_line=1, start_col=0,
        end_line=root.end_point[0] + 1, end_col=root.end_point[1],
        body_hash=file.sha, extractor=EXTRACTOR,
    ))

    def decorators_of(deco_node):
        """Return (name, node) for each decorator on a decorated_definition."""
        out = []
        for c in deco_node.children:
            if c.type == "decorator":
                expr = c.children[1] if len(c.children) > 1 else None
                if expr is not None:
                    name = _dotted_tail(src, expr)
                    if name:
                        out.append((name, c))
        return out

    def handle_class(node, parent_fqn, container_id, decos):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = text(src, name_node)
        fqn = f"{parent_fqn}.{name}" if parent_fqn else name
        cid = make_id(repo, fqn, "class")
        deco_names = [d for d, _ in decos]
        nodes.append(Node(
            id=cid, label="Class", name=name, fqn=fqn, repo=repo, kind="class",
            lang="python", file=file.relpath,
            start_line=node.start_point[0] + 1, start_col=node.start_point[1],
            end_line=node.end_point[0] + 1, end_col=node.end_point[1],
            visibility=_visibility(name), modifiers=deco_names,
            is_abstract="abstractmethod" in deco_names or _has_abc_base(src, node),
            docstring=_docstring(src, node), body_hash=body_hash(text(src, node)),
            extractor=EXTRACTOR,
        ))
        contains(container_id, node, cid)
        for dname, dnode in decos:
            ref("ANNOTATED_WITH", cid, dname, "annotation", dnode)
        supers = node.child_by_field_name("superclasses")
        if supers:
            for c in supers.children:
                if c.type in ("identifier", "attribute"):
                    ref("EXTENDS", cid, _dotted_tail(src, c), "type", c)
        body = node.child_by_field_name("body")
        if body:
            walk_block(body, fqn, cid, in_class=True)

    def handle_function(node, parent_fqn, container_id, in_class, decos):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = text(src, name_node)
        fqn = f"{parent_fqn}.{name}" if parent_fqn else name
        params_node = node.child_by_field_name("parameters")
        params = text(src, params_node) if params_node else "()"
        rt_node = node.child_by_field_name("return_type")
        return_type = text(src, rt_node) if rt_node is not None else ""
        deco_names = [d for d, _ in decos]
        is_static = "staticmethod" in deco_names
        is_classmethod = "classmethod" in deco_names
        kind = "function" if not in_class else ("method")
        body = node.child_by_field_name("body")
        branch_count, loop_count = _complexity_counts(body)
        cyclomatic = 1 + branch_count + loop_count
        pnames, ptypes = _params(src, params_node, in_class)
        mid = make_id(repo, fqn, "method" if in_class else "function")
        nodes.append(Node(
            id=mid, label="Function", name=name, fqn=fqn, repo=repo, kind=kind,
            lang="python", file=file.relpath,
            start_line=node.start_point[0] + 1, start_col=node.start_point[1],
            end_line=node.end_point[0] + 1, end_col=node.end_point[1],
            visibility=_visibility(name), modifiers=deco_names,
            is_static=is_static or is_classmethod,
            is_abstract="abstractmethod" in deco_names,
            is_async=_is_async(node),
            return_type=return_type, param_count=_param_count(params_node, in_class),
            param_names=pnames, param_types=ptypes,
            signature=f"{name}{params}", docstring=_docstring(src, node),
            loc=(node.end_point[0] - node.start_point[0]) + 1,
            cyclomatic=cyclomatic,
            branch_count=branch_count,
            loop_count=loop_count,
            body_hash=body_hash(text(src, node)), extractor=EXTRACTOR,
        ))
        contains(container_id, node, mid)
        for dname, dnode in decos:
            ref("ANNOTATED_WITH", mid, dname, "annotation", dnode)
            for method, route in _endpoint_specs(src, dnode):
                emit_endpoint(method, route, mid, dnode)
        # type edges: return annotation + parameter annotations
        if rt_node is not None:
            _emit_type(ref, "RETURNS", mid, src, rt_node)
        if params_node is not None:
            for p in params_node.children:
                if p.type in ("typed_parameter", "typed_default_parameter"):
                    t = p.child_by_field_name("type")
                    if t is not None:
                        _emit_type(ref, "HAS_TYPE", mid, src, t)
        if body:
            receiver_types = _local_receiver_types(src, body)
            # Only calls directly in this function's scope — nested defs are
            # walked separately so their calls aren't double-attributed here.
            for d in _calls_in_scope(body):
                fn = d.child_by_field_name("function")
                if fn is not None:
                    callee = _dotted_tail(src, fn)
                    if callee:
                        ref(
                            "CALLS",
                            mid,
                            callee,
                            "call",
                            fn,
                            recv=_receiver(src, fn),
                            call_arity=_call_arity(d),
                            recv_type=_infer_receiver_type(src, fn, receiver_types),
                        )
                    ob = _outbound_call(src, d)
                    if ob is not None:
                        emit_api_call(mid, ob[0], ob[1], fn)
            _emit_exceptions(body, mid)
            if in_class:
                _emit_state(body, mid, parent_fqn, container_id)
            walk_block(body, fqn, mid, in_class=False)
        return mid

    def walk_block(block, parent_fqn, container_id, in_class):
        for stmt in block.children:
            node = stmt
            decos = []
            if stmt.type == "decorated_definition":
                node = stmt.child_by_field_name("definition")
                decos = decorators_of(stmt)
                if node is None:
                    continue
            if node.type == "class_definition":
                handle_class(node, parent_fqn, container_id, decos)
            elif node.type == "function_definition":
                handle_function(node, parent_fqn, container_id, in_class, decos)
            elif node.type == "expression_statement" and in_class:
                _maybe_field(node, parent_fqn, container_id)

    def _maybe_field(stmt, class_fqn, class_id):
        for c in stmt.children:
            if c.type in ("assignment",):
                left = c.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    fname = text(src, left)
                    ffqn = f"{class_fqn}.{fname}"
                    fid = make_id(repo, ffqn, "field")
                    if fid in created_fields:
                        continue
                    created_fields.add(fid)
                    type_node = c.child_by_field_name("type")
                    nodes.append(Node(
                        id=fid, label="Field", name=fname, fqn=ffqn, repo=repo,
                        kind="field", lang="python", file=file.relpath,
                        start_line=stmt.start_point[0] + 1, start_col=stmt.start_point[1],
                        end_line=stmt.end_point[0] + 1, end_col=stmt.end_point[1],
                        visibility=_visibility(fname),
                        return_type=text(src, type_node) if type_node else "",
                        extractor=EXTRACTOR,
                    ))
                    contains(class_id, stmt, fid)
                    if type_node is not None:
                        _emit_type(ref, "OF_TYPE", fid, src, type_node)

    def _emit_exceptions(body, fn_id):
        for n in _scope_walk(body):
            if n.type == "raise_statement":
                for c in n.children:
                    if c.type in ("raise", "from"):
                        continue
                    nm = _dotted_tail(src, c)
                    if nm:
                        ref("THROWS", fn_id, nm, "type", c)
                    break
            elif n.type == "except_clause":
                for c in n.children:
                    if c.type in ("except", ":", "as", "block", "comment"):
                        continue
                    for nm in _collect_type_names(src, c):
                        ref("CATCHES", fn_id, nm, "type", c)
                    break

    def _ensure_field(class_fqn, class_id, name, node):
        ffqn = f"{class_fqn}.{name}"
        fid = make_id(repo, ffqn, "field")
        if fid not in created_fields:
            created_fields.add(fid)
            nodes.append(Node(
                id=fid, label="Field", name=name, fqn=ffqn, repo=repo, kind="field",
                lang="python", file=file.relpath,
                start_line=node.start_point[0] + 1, start_col=node.start_point[1],
                end_line=node.end_point[0] + 1, end_col=node.end_point[1],
                visibility=_visibility(name), extractor=EXTRACTOR))
            contains(class_id, node, fid)
        return fid

    def _emit_state(body, fn_id, class_fqn, class_id):
        """READS/WRITES of `self.<field>` within a method (cross-object deferred)."""
        write_ids = set()
        for n in _scope_walk(body):
            if n.type in ("assignment", "augmented_assignment"):
                left = n.child_by_field_name("left")
                if left is None:
                    continue
                for a in _assign_targets(src, left):
                    write_ids.add(a.id)   # tree-sitter node id (stable across wrappers)
                    name = _self_attr_name(src, a)
                    _ensure_field(class_fqn, class_id, name, a)
                    ref("WRITES", fn_id, name, "field", a, recv="self")
                    if n.type == "augmented_assignment":
                        ref("READS", fn_id, name, "field", a, recv="self")
        for n in _scope_walk(body):
            if n.type == "attribute" and _self_attr_name(src, n) and n.id not in write_ids:
                name = _self_attr_name(src, n)
                _ensure_field(class_fqn, class_id, name, n)
                ref("READS", fn_id, name, "field", n, recv="self")

    # top-level imports
    for child in root.children:
        if child.type == "import_statement":
            for c in child.children:
                if c.type in ("dotted_name", "aliased_import"):
                    ref(
                        "IMPORTS",
                        file_id,
                        _dotted_tail(src, c),
                        "import",
                        c,
                        import_fqn=_import_full_name(src, c),
                    )
        elif child.type == "import_from_statement":
            mod = child.child_by_field_name("module_name")
            if mod is not None:
                ref(
                    "IMPORTS",
                    file_id,
                    _dotted_tail(src, mod),
                    "import",
                    mod,
                    import_fqn=text(src, mod).strip(),
                )

    walk_block(root, module_fqn, file_id, in_class=False)
    return nodes, edges, refs


def _emit_type(ref, primary: str, src_id: str, src: bytes, type_node) -> None:
    """Emit `primary` to the base type and HAS_GENERIC to each generic arg."""
    names = _collect_type_names(src, type_node)
    if not names:
        return
    ref(primary, src_id, names[0], "type", type_node)
    for g in names[1:]:
        ref("HAS_GENERIC", src_id, g, "type", type_node)


def _collect_type_names(src: bytes, node) -> list[str]:
    """Ordered type names in an annotation: List[User] -> [List, User];
    Optional["Finding"] -> [Optional, Finding]; pkg.Mod.T -> [T]."""
    out: list[str] = []

    def walk(n):
        if n.type == "identifier":
            out.append(text(src, n))
        elif n.type == "attribute":
            tail = _dotted_tail(src, n)
            if tail:
                out.append(tail)
        elif n.type == "string":
            s = _strip_quotes(text(src, n))
            if s and s.isidentifier():
                out.append(s)
        else:
            for c in n.children:
                walk(c)

    walk(node)
    return out


def _is_async(node) -> bool:
    return any(c.type == "async" for c in node.children)


def _param_count(params_node, in_class: bool) -> int:
    """Count declared formal parameters (excluding the implicit self/cls)."""
    if params_node is None:
        return 0
    kinds = {
        "identifier", "typed_parameter", "default_parameter",
        "typed_default_parameter", "list_splat_pattern", "dictionary_splat_pattern",
    }
    params = [c for c in params_node.children if c.type in kinds]
    if in_class and params and _first_is_selfish(params[0]):
        params = params[1:]
    return len(params)


def _first_is_selfish(node) -> bool:
    txt = node.text.decode("utf-8", "replace") if node.text else ""
    head = txt.split(":")[0].split("=")[0].strip()
    return head in ("self", "cls")


def _splat_inner(src: bytes, node) -> str:
    for c in node.children:
        if c.type == "identifier":
            return text(src, c)
    return ""


def _params(src: bytes, params_node, in_class: bool):
    """Ordered (names, types) for input parameters, excluding self/cls.
    Variadics keep their sigil (`*args`, `**kwargs`); untyped -> ''."""
    names: list[str] = []
    types: list[str] = []
    if params_node is None:
        return names, types
    kinds = {
        "identifier", "typed_parameter", "default_parameter",
        "typed_default_parameter", "list_splat_pattern", "dictionary_splat_pattern",
    }
    params = [c for c in params_node.children if c.type in kinds]
    if in_class and params and _first_is_selfish(params[0]):
        params = params[1:]
    for p in params:
        nm, tp = "", ""
        if p.type == "identifier":
            nm = text(src, p)
        elif p.type == "list_splat_pattern":
            nm = "*" + _splat_inner(src, p)
        elif p.type == "dictionary_splat_pattern":
            nm = "**" + _splat_inner(src, p)
        elif p.type == "typed_parameter":
            for cc in p.children:
                if cc.type == "identifier":
                    nm = text(src, cc)
                elif cc.type == "list_splat_pattern":
                    nm = "*" + _splat_inner(src, cc)
                elif cc.type == "dictionary_splat_pattern":
                    nm = "**" + _splat_inner(src, cc)
            t = p.child_by_field_name("type")
            if t is not None:
                tp = text(src, t).strip()
        elif p.type in ("default_parameter", "typed_default_parameter"):
            n_ = p.child_by_field_name("name")
            nm = text(src, n_) if n_ is not None else ""
            t = p.child_by_field_name("type")
            if t is not None:
                tp = text(src, t).strip()
        if nm:
            names.append(nm)
            types.append(tp)
    return names, types


def _docstring(src: bytes, def_node) -> str:
    body = def_node.child_by_field_name("body")
    if body is None:
        return ""
    for c in body.children:
        if c.type == "expression_statement" and c.children and c.children[0].type == "string":
            raw = text(src, c.children[0])
            return _strip_quotes(raw)[:500]
        if c.type not in ("comment",):
            break
    return ""


def _strip_quotes(s: str) -> str:
    for q in ('"""', "'''", '"', "'"):
        if s.startswith(q) and s.endswith(q) and len(s) >= 2 * len(q):
            return s[len(q):-len(q)].strip()
    return s.strip()


def _has_abc_base(src: bytes, class_node) -> bool:
    supers = class_node.child_by_field_name("superclasses")
    if not supers:
        return False
    txt = text(src, supers)
    return "ABC" in txt or "ABCMeta" in txt


def _scope_walk(block):
    """Yield nodes lexically inside `block` but not inside a nested function/class
    definition (those are extracted with their own scope)."""
    stack = list(reversed(block.children))
    while stack:
        cur = stack.pop()
        if cur.type in ("function_definition", "class_definition", "decorated_definition"):
            continue
        yield cur
        if cur.children:
            stack.extend(reversed(cur.children))


def _calls_in_scope(block):
    return (n for n in _scope_walk(block) if n.type == "call")


def _self_attr_name(src: bytes, node) -> str:
    """If `node` is `self.<x>` / `cls.<x>`, return 'x'; else ''."""
    if node.type != "attribute":
        return ""
    obj = node.child_by_field_name("object")
    if obj is None or obj.type != "identifier" or text(src, obj) not in ("self", "cls"):
        return ""
    attr = node.child_by_field_name("attribute")
    return text(src, attr) if attr else ""


def _assign_targets(src: bytes, left):
    """Direct `self.x` assignment targets (incl. tuple-unpacking targets)."""
    if left.type == "attribute" and _self_attr_name(src, left):
        return [left]
    if left.type in ("pattern_list", "tuple", "tuple_pattern", "list_pattern"):
        return [c for c in left.children
                if c.type == "attribute" and _self_attr_name(src, c)]
    return []


def _receiver(src: bytes, fn) -> str:
    """The call receiver's tail name: 'self'/'cls', a module/class/var name, or
    '' for a bare `foo()`. For `a.b.foo()` returns 'b' (the immediate object)."""
    if fn.type != "attribute":
        return ""
    obj = fn.child_by_field_name("object")
    return _dotted_tail(src, obj) if obj is not None else ""


def _call_arity(call_node) -> int:
    """Best-effort positional+keyword argument count for a call expression."""
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return -1
    count = 0
    for c in args.children:
        if c.type in ("(", ")", ","):
            continue
        count += 1
    return count


def _string_text(src: bytes, node) -> str:
    """The literal value of a `string` node, without quotes/prefix."""
    if node.type != "string":
        return ""
    for c in node.children:
        if c.type == "string_content":
            return text(src, c)
    return text(src, node).strip("'\"")


def _positional_args(call_node):
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return []
    return [c for c in args.children
            if c.type not in ("(", ")", ",", "keyword_argument", "comment")]


def _string_arg(src: bytes, call_node, index: int) -> str:
    pos = _positional_args(call_node)
    if index < len(pos) and pos[index].type == "string":
        return _string_text(src, pos[index])
    return ""


def _methods_kwarg(src: bytes, call_node) -> list[str]:
    """HTTP verbs from a `methods=[...]` keyword arg (Flask-style routes)."""
    args = call_node.child_by_field_name("arguments")
    out: list[str] = []
    if args is None:
        return out
    for c in args.children:
        if c.type != "keyword_argument":
            continue
        name = c.child_by_field_name("name")
        val = c.child_by_field_name("value")
        if name is None or val is None or text(src, name) != "methods":
            continue
        for el in val.children:
            if el.type == "string":
                m = _string_text(src, el).lower()
                if m in HTTP_METHODS:
                    out.append(m)
    return out


def _endpoint_specs(src: bytes, deco_node) -> list[tuple[str, str]]:
    """Route definitions on a decorator: FastAPI/Flask `@app.get('/x')`,
    `@router.post('/x')`, `@app.route('/x', methods=['POST'])`."""
    expr = deco_node.children[1] if len(deco_node.children) > 1 else None
    if expr is None or expr.type != "call":
        return []
    fn = expr.child_by_field_name("function")
    if fn is None or fn.type != "attribute":
        return []
    tail = _dotted_tail(src, fn)
    route = _string_arg(src, expr, 0)
    if not route:
        return []
    if tail in HTTP_METHODS:
        return [(tail.upper(), route)]
    if tail in ("route", "api_route"):
        methods = _methods_kwarg(src, expr) or ["get"]
        return [(m.upper(), route) for m in methods]
    return []


def _outbound_call(src: bytes, call_node):
    """If this call is an outbound HTTP request, return (METHOD, url-literal)."""
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "identifier" and text(src, fn) == "urlopen":
        url = _string_arg(src, call_node, 0)
        return ("GET", url) if url else None
    if fn.type != "attribute":
        return None
    tail = _dotted_tail(src, fn)
    recv = _receiver(src, fn)
    if recv not in HTTP_CLIENT_RECEIVERS:
        return None
    if tail in HTTP_METHODS:
        url = _string_arg(src, call_node, 0)
        return (tail.upper(), url) if url else None
    if tail == "request":
        verb = _string_arg(src, call_node, 0)
        url = _string_arg(src, call_node, 1)
        return (verb.upper(), url) if verb and url else None
    return None


def _import_full_name(src: bytes, node) -> str:
    raw = text(src, node).strip()
    if " as " in raw:
        raw = raw.split(" as ", 1)[0].strip()
    return raw


def _local_receiver_types(src: bytes, body) -> dict[str, str]:
    """Infer simple local variable -> class type bindings in function scope."""
    out: dict[str, str] = {}
    for n in _scope_walk(body):
        if n.type == "assignment":
            left = n.child_by_field_name("left")
            right = n.child_by_field_name("right")
            if left is None or right is None or left.type != "identifier":
                continue
            var = text(src, left)
            tp = _inferred_type_name(src, right)
            if var and tp:
                out[var] = tp
        elif n.type == "annotated_assignment":
            left = n.child_by_field_name("left")
            anno = n.child_by_field_name("type")
            if left is None or anno is None or left.type != "identifier":
                continue
            var = text(src, left)
            tp = _dotted_tail(src, anno)
            if var and tp:
                out[var] = tp
    return out


def _inferred_type_name(src: bytes, node) -> str:
    if node.type == "call":
        fn = node.child_by_field_name("function")
        if fn is None:
            return ""
        return _dotted_tail(src, fn)
    if node.type in ("identifier", "attribute"):
        return _dotted_tail(src, node)
    return ""


def _infer_receiver_type(src: bytes, fn_node, local_types: dict[str, str]) -> str:
    if fn_node.type != "attribute":
        return ""
    obj = fn_node.child_by_field_name("object")
    if obj is None:
        return ""
    if obj.type == "identifier":
        name = text(src, obj)
        return local_types.get(name, "")
    return ""


def _complexity_counts(body):
    if body is None:
        return 0, 0
    branch_nodes = {"if_statement", "conditional_expression", "match_statement", "except_clause"}
    loop_nodes = {"for_statement", "while_statement"}
    branch_count = 0
    loop_count = 0
    for n in _scope_walk(body):
        if n.type in branch_nodes:
            branch_count += 1
        if n.type in loop_nodes:
            loop_count += 1
    return branch_count, loop_count


def _dotted_tail(src: bytes, node) -> str:
    """Return the trailing simple name of an identifier/attribute/dotted_name."""
    t = node.type
    if t == "identifier":
        return text(src, node)
    if t == "attribute":
        attr = node.child_by_field_name("attribute")
        return text(src, attr) if attr else ""
    if t in ("dotted_name", "aliased_import", "scoped_identifier"):
        ids = [c for c in node.children if c.type in ("identifier", "dotted_name")]
        if ids:
            return text(src, ids[-1])
        return text(src, node)
    if t == "call":
        fn = node.child_by_field_name("function")
        return _dotted_tail(src, fn) if fn else ""
    return text(src, node).strip()
