"""Stage 1 — Java structural extraction via tree-sitter.

Emits:
    Nodes: File, Class(kind=class|interface|enum|record), Function(kind=method|
           constructor), Field — with metadata (range incl. columns, visibility,
           modifiers, is_static/abstract, return_type, param_count).
    Edges (resolved): CONTAINS — with provenance.
        RawRefs (name-only): IMPORTS, EXTENDS, IMPLEMENTS, CALLS, INSTANTIATES,
            ANNOTATED_WITH — each carrying the reference-site location.
"""
from __future__ import annotations

import os

from ..apispec import (
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
from .common import iter_descendants, simple_type_name, text

EXTRACTOR = "tree-sitter"

# Spring mapping annotations -> HTTP method.
_SPRING_MAPPING = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
}
# RestTemplate method -> HTTP verb (outbound call detection).
_REST_TEMPLATE_CALLS = {
    "getForObject": "GET", "getForEntity": "GET",
    "postForObject": "POST", "postForEntity": "POST", "postForLocation": "POST",
    "put": "PUT", "delete": "DELETE",
}

_AUTH_REQUIRE_ANNOTATIONS = {
    "Authenticated", "AuthenticationPrincipal", "LoginRequired",
}
# Spring/Jakarta DI annotations that mark a field/constructor param as an
# injected dependency -> emitted as an AUTOWIRED type-shaped ref (schema.py
# already reserves this edge type; nothing emitted it until now). Ported from
# primitive-pr's Java resolution work (same annotation set, same single-
# constructor inference rule) so graph_rag gets equivalent DI-edge coverage.
_JAVA_AUTOWIRE_ANNOTATIONS = {"Autowired", "Inject", "Resource"}
_POLICY_ANNOTATIONS = {
    "PreAuthorize", "Secured", "RolesAllowed", "PermissionsAllowed",
}
_EVENT_CONSUMER_ANNOTATIONS = {
    "KafkaListener", "RabbitListener", "JmsListener", "SqsListener",
}
_EVENT_EMIT_METHODS = {
    "publish", "emit", "send", "produce", "dispatch", "publishEvent",
}
_EVENT_CONSUME_METHODS = {
    "subscribe", "consume", "listen", "registerListener", "on",
}

_TYPE_DECLS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
}

_VISIBILITY = {"public", "private", "protected"}
_MODIFIER_KEYWORDS = {
    "static", "final", "abstract", "synchronized", "native", "transient",
    "volatile", "default", "sealed", "strictfp",
}


def extract(file: FileInfo, repo: str):
    src = file.source
    tree = get_parser("java").parse(src)
    root = tree.root_node

    nodes: list[Node] = []
    edges: list[Edge] = []
    refs: list[RawRef] = []

    def contains(container_id: str, child_node, child_id: str):
        edges.append(Edge(
            "CONTAINS", container_id, child_id,
            origin=Origin.EXTRACTED.value, extractor=EXTRACTOR,
            evidence_file=file.relpath,
            evidence_line=child_node.start_point[0] + 1,
            evidence_col=child_node.start_point[1],
        ))

    def defines(container_id: str, child_node, child_id: str):
        # Add semantic ownership without replacing structural containment.
        edges.append(Edge(
            "DEFINES", container_id, child_id,
            origin=Origin.EXTRACTED.value, extractor=EXTRACTOR,
            evidence_file=file.relpath,
            evidence_line=child_node.start_point[0] + 1,
            evidence_col=child_node.start_point[1],
        ))

    def ref(rtype, src_id, target, kind_hint, node, recv="", call_arity=-1, arg_names=None):
        refs.append(RawRef(
            rtype, src_id, target, kind_hint, recv=recv,
            ref_file=file.relpath,
            ref_line=node.start_point[0] + 1, ref_col=node.start_point[1],
            call_arity=call_arity,
            arg_names=list(arg_names or []),
        ))

    def emit_endpoint(method, route, handler_id, ev_node):
        eid = endpoint_id(repo, method, route)
        nodes.append(Node(
            id=eid, label="Endpoint", name=endpoint_display(method, route),
            fqn=endpoint_fqn(method, route), repo=repo, kind="endpoint",
            lang="java", file=file.relpath,
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
        host, path = split_url(url)
        refs.append(RawRef(
            "CALLS_API", handler_id, path, "api", recv=host, http_method=method,
            ref_file=file.relpath,
            ref_line=ev_node.start_point[0] + 1, ref_col=ev_node.start_point[1],
        ))

    file_fqn = file.relpath
    file_id = make_id(repo, file_fqn, "file")

    package = ""
    for child in root.children:
        if child.type == "package_declaration":
            for c in child.children:
                if c.type in ("scoped_identifier", "identifier"):
                    package = text(src, c)
        elif child.type == "import_declaration":
            name, fqn, is_wildcard = _import_name(src, child)
            if name:
                if is_wildcard:
                    # `import com.foo.util.*;` -- target_name marks the wildcard
                    # explicitly (trailing ".*") so the resolver can expand it
                    # to every in-repo class under that package, instead of
                    # being mistaken for an import of a class literally named
                    # after the package's last segment.
                    ref("IMPORTS", file_id, f"{name}.*", "import_wildcard", child)
                    refs[-1].import_fqn = name
                else:
                    ref("IMPORTS", file_id, name, "import", child)
                    refs[-1].import_fqn = fqn

    nodes.append(Node(
        id=file_id, label="File", name=os.path.basename(file.relpath),
        fqn=file_fqn, repo=repo, kind="file", lang="java", file=file.relpath,
        package=package, start_line=1, start_col=0, end_line=root.end_point[0] + 1,
        end_col=root.end_point[1], body_hash=file.sha, extractor=EXTRACTOR,
    ))

    def modifiers_of(node):
        """Return (visibility, [modifier keywords], [annotation names])."""
        vis, mods, anns = "", [], []
        for child in node.children:
            if child.type == "modifiers":
                for m in child.children:
                    if m.type in _VISIBILITY:
                        vis = m.type
                    elif m.type in _MODIFIER_KEYWORDS:
                        mods.append(m.type)
                    elif m.type in ("annotation", "marker_annotation"):
                        nm = m.child_by_field_name("name")
                        if nm:
                            anns.append(simple_type_name(text(src, nm)))
        return (vis or "package"), mods, anns

    def walk_type(node, parent_fqn, container_id):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = text(src, name_node)
        fqn = f"{parent_fqn}.{name}" if parent_fqn else name
        kind = _TYPE_DECLS[node.type]
        vis, mods, anns = modifiers_of(node)
        cid = make_id(repo, fqn, kind)
        nodes.append(Node(
            id=cid, label="Class", name=name, fqn=fqn, repo=repo, kind=kind,
            lang="java", file=file.relpath,
            start_line=node.start_point[0] + 1, start_col=node.start_point[1],
            end_line=node.end_point[0] + 1, end_col=node.end_point[1],
            visibility=vis, modifiers=mods, is_abstract="abstract" in mods,
            is_static="static" in mods, body_hash=body_hash(text(src, node)),
            extractor=EXTRACTOR,
        ))
        contains(container_id, node, cid)
        defines(container_id, node, cid)
        for ann in anns:
            ref("ANNOTATED_WITH", cid, ann, "annotation", node)
        for et in _java_auth_policy_specs(src, node):
            ref(et[0], cid, et[1], "policy", node)

        sc = node.child_by_field_name("superclass")
        if sc:
            for t in _types_in(src, sc):
                ref("EXTENDS", cid, t, "type", sc)
        si = node.child_by_field_name("interfaces")
        if si:
            for t in _types_in(src, si):
                ref("IMPLEMENTS", cid, t, "type", si)

        route_prefix = _request_mapping_prefix(src, node)
        body = node.child_by_field_name("body")
        if body:
            members = body.children
            constructors = [c for c in members if c.type == "constructor_declaration"]
            for child in members:
                if child.type in ("method_declaration", "constructor_declaration"):
                    walk_method(child, fqn, cid, route_prefix)
                    if child.type == "constructor_declaration":
                        _extract_ctor_di(child, cid, single=len(constructors) == 1)
                elif child.type == "field_declaration":
                    walk_field(child, fqn, cid)
                elif child.type in _TYPE_DECLS:
                    walk_type(child, fqn, cid)

    def _extract_ctor_di(ctor_node, class_id, single: bool):
        """DI edge for constructor-injected dependencies: emitted when the
        constructor is itself @Autowired/@Inject, OR it's the class's only
        constructor (Spring's implicit-injection convention since 4.3 — no
        annotation needed on a single constructor)."""
        _, _, ctor_anns = modifiers_of(ctor_node)
        if not single and not set(ctor_anns) & _JAVA_AUTOWIRE_ANNOTATIONS:
            return
        params_node = ctor_node.child_by_field_name("parameters")
        if params_node is None:
            return
        for p in params_node.children:
            if p.type not in ("formal_parameter", "spread_parameter"):
                continue
            t = p.child_by_field_name("type")
            if t is not None:
                ref("AUTOWIRED", class_id, simple_type_name(text(src, t)), "type", p)

    def walk_method(node, class_fqn, class_id, route_prefix=""):
        name_node = node.child_by_field_name("name")
        is_ctor = node.type == "constructor_declaration"
        name = text(src, name_node) if name_node else (class_fqn.rsplit(".", 1)[-1] if is_ctor else "<anon>")
        params_node = node.child_by_field_name("parameters")
        params = text(src, params_node) if params_node else "()"
        signature = f"{name}{params}"
        vis, mods, anns = modifiers_of(node)
        rt_node = node.child_by_field_name("type")
        return_type = simple_type_name(text(src, rt_node)) if rt_node is not None else ""
        body = node.child_by_field_name("body")
        branch_count, loop_count = _complexity_counts(body)
        cyclomatic = 1 + branch_count + loop_count
        _pnames, _ptypes = _params(src, params_node)
        fqn = f"{class_fqn}#{name}"
        mid = make_id(repo, f"{fqn}{params}", "method")
        nodes.append(Node(
            id=mid, label="Function", name=name, fqn=fqn, repo=repo,
            kind="constructor" if is_ctor else "method", lang="java",
            file=file.relpath,
            start_line=node.start_point[0] + 1, start_col=node.start_point[1],
            end_line=node.end_point[0] + 1, end_col=node.end_point[1],
            visibility=vis, modifiers=mods, is_static="static" in mods,
            is_abstract="abstract" in mods, return_type=return_type,
            param_count=_param_count(params_node),
            param_names=_pnames, param_types=_ptypes, signature=signature,
            loc=(node.end_point[0] - node.start_point[0]) + 1,
            cyclomatic=cyclomatic,
            branch_count=branch_count,
            loop_count=loop_count,
            body_hash=body_hash(text(src, node)), extractor=EXTRACTOR,
        ))
        contains(class_id, node, mid)
        defines(class_id, node, mid)
        for ann in anns:
            ref("ANNOTATED_WITH", mid, ann, "annotation", node)
        for et in _java_auth_policy_specs(src, node):
            ref(et[0], mid, et[1], "policy", node)
        for topic in _java_event_consumer_topics(src, node):
            ref("CONSUMES_EVENT", mid, topic, "event", node)
        for method, route in _spring_endpoints(src, node, route_prefix):
            emit_endpoint(method, route, mid, node)
        # type edges: return type + parameter types
        if rt_node is not None and not is_ctor:
            _emit_type(ref, "RETURNS", mid, src, rt_node)
        if params_node is not None:
            for p in params_node.children:
                if p.type in ("formal_parameter", "spread_parameter"):
                    t = p.child_by_field_name("type")
                    if t is not None:
                        _emit_type(ref, "HAS_TYPE", mid, src, t)

        # checked exceptions declared in the `throws` clause
        for c in node.children:
            if c.type == "throws":
                for t in _types_in(src, c):
                    ref("THROWS", mid, t, "type", c)

        if body:
            # field-writes: `this.x = ...` (LHS of an assignment)
            write_fa = set()
            for d in iter_descendants(body):
                if d.type == "assignment_expression":
                    left = d.child_by_field_name("left")
                    if left is not None and _this_field(src, left):
                        write_fa.add(left.id)   # stable tree-sitter node id
            for d in iter_descendants(body):
                if d.type == "method_invocation":
                    nm = d.child_by_field_name("name")
                    if nm:
                        pass_args = _call_arg_names(src, d)
                        ref(
                            "CALLS",
                            mid,
                            text(src, nm),
                            "call",
                            d,
                            call_arity=_call_arity(d),
                        )
                        if pass_args:
                            ref(
                                "PASSES",
                                mid,
                                text(src, nm),
                                "call",
                                d,
                                call_arity=_call_arity(d),
                                arg_names=pass_args,
                            )
                        ob = _outbound_java(src, d, text(src, nm))
                        if ob is not None:
                            emit_api_call(mid, ob[0], ob[1], d)
                        ev = _outbound_event_java(src, d, text(src, nm))
                        if ev:
                            ref("EMITS_EVENT", mid, ev, "event", d)
                        evc = _inbound_event_java(src, d, text(src, nm))
                        if evc:
                            ref("CONSUMES_EVENT", mid, evc, "event", d)
                elif d.type == "object_creation_expression":
                    tp = d.child_by_field_name("type")
                    if tp:
                        ref("INSTANTIATES", mid, simple_type_name(text(src, tp)), "type", d)
                elif d.type == "throw_statement":
                    for t in _thrown_types(src, d):
                        ref("THROWS", mid, t, "type", d)
                elif d.type == "catch_formal_parameter":
                    for t in _types_in(src, d):
                        ref("CATCHES", mid, t, "type", d)
                elif d.type == "field_access":
                    fname = _this_field(src, d)
                    if fname:
                        ref("WRITES" if d.id in write_fa else "READS",
                            mid, fname, "field", d, recv="this")

    def walk_field(node, class_fqn, class_id):
        vis, mods, anns = modifiers_of(node)
        type_node = node.child_by_field_name("type")
        ftype = simple_type_name(text(src, type_node)) if type_node is not None else ""
        if ftype and set(anns) & _JAVA_AUTOWIRE_ANNOTATIONS:
            ref("AUTOWIRED", class_id, ftype, "type", node)
        for d in node.children:
            if d.type == "variable_declarator":
                nm = d.child_by_field_name("name")
                if nm:
                    fname = text(src, nm)
                    ffqn = f"{class_fqn}.{fname}"
                    fid = make_id(repo, ffqn, "field")
                    nodes.append(Node(
                        id=fid, label="Field", name=fname, fqn=ffqn, repo=repo,
                        kind="field", lang="java", file=file.relpath,
                        start_line=node.start_point[0] + 1, start_col=node.start_point[1],
                        end_line=node.end_point[0] + 1, end_col=node.end_point[1],
                        visibility=vis, modifiers=mods, is_static="static" in mods,
                        return_type=ftype, extractor=EXTRACTOR,
                    ))
                    contains(class_id, node, fid)
                    defines(class_id, node, fid)
                    if type_node is not None:
                        _emit_type(ref, "OF_TYPE", fid, src, type_node)

    for child in root.children:
        if child.type in _TYPE_DECLS:
            walk_type(child, package, file_id)

    return nodes, edges, refs


_PRIMITIVES = {
    "void", "int", "long", "double", "float", "boolean", "char", "byte", "short",
}


def _emit_type(ref, primary: str, src_id: str, src: bytes, type_node) -> None:
    """Emit `primary` to the base type and HAS_GENERIC to each generic arg
    (skipping primitives, which can never be an in-repo Class)."""
    base, args = _java_type_parts(src, type_node)
    if base and base not in _PRIMITIVES:
        ref(primary, src_id, base, "type", type_node)
    for g in args:
        if g not in _PRIMITIVES:
            ref("HAS_GENERIC", src_id, g, "type", type_node)


def _java_type_parts(src: bytes, node):
    """Return (base simple-name, [generic-arg simple-names]) for a type node."""
    base = simple_type_name(text(src, node))
    args = []
    for d in iter_descendants(node):
        if d.type == "type_arguments":
            for a in d.children:
                if a.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
                    nm = simple_type_name(text(src, a))
                    if nm and nm not in args:
                        args.append(nm)
    return base, args


def _this_field(src: bytes, fa) -> str:
    """If `fa` is `this.<x>`, return 'x'; else ''."""
    if fa.type != "field_access":
        return ""
    obj = fa.child_by_field_name("object")
    if obj is None or obj.type != "this":
        return ""
    f = fa.child_by_field_name("field")
    return text(src, f) if f else ""


def _thrown_types(src: bytes, throw_node):
    """Exception type names from `throw new X(...)` (rethrown vars are skipped)."""
    out = []
    for d in [throw_node, *iter_descendants(throw_node)]:
        if d.type == "object_creation_expression":
            tp = d.child_by_field_name("type")
            if tp is not None:
                nm = simple_type_name(text(src, tp))
                if nm:
                    out.append(nm)
    return out


def _param_count(params_node) -> int:
    if params_node is None:
        return 0
    return sum(1 for c in params_node.children
               if c.type in ("formal_parameter", "spread_parameter"))


def _params(src: bytes, params_node):
    """Ordered (names, types) for a method's formal parameters. Varargs keep
    a trailing `...` on the type."""
    names: list[str] = []
    types: list[str] = []
    if params_node is None:
        return names, types
    for c in params_node.children:
        if c.type not in ("formal_parameter", "spread_parameter"):
            continue
        t = c.child_by_field_name("type")
        tp = simple_type_name(text(src, t)) if t is not None else ""
        n_ = c.child_by_field_name("name")
        if n_ is None:  # spread_parameter holds its name in a variable_declarator
            vd = next((cc for cc in c.children if cc.type == "variable_declarator"), None)
            n_ = vd.child_by_field_name("name") if vd is not None else None
        nm = text(src, n_) if n_ is not None else ""
        if c.type == "spread_parameter" and tp:
            tp += "..."
        names.append(nm)
        types.append(tp)
    return names, types


def _import_name(src: bytes, node) -> tuple[str, str, bool]:
    # Returns (target_name, fully_qualified_path, is_wildcard).
    # `import a.b.C;`  -> ("C", "a.b.C", False) -- target_name is the simple
    #     class name (what call/type sites reference), fqn is the full path
    #     (what the resolver uses to disambiguate same-named classes in
    #     different packages).
    # `import a.b.*;`  -> ("a.b", "a.b", True) -- the wildcard's package prefix.
    # `import static a.B.x;` -> treated like a normal import of `a.B` (the
    #     static member itself isn't a class we model).
    is_wildcard = any(c.type == "asterisk" for c in node.children)
    scoped = None
    for c in node.children:
        if c.type in ("scoped_identifier", "identifier"):
            scoped = c
    if scoped is None:
        return "", "", False
    full = text(src, scoped)
    if is_wildcard:
        return full, full, True
    tail = full.rsplit(".", 1)[-1]
    return tail, full, False


def _call_arity(call_node) -> int:
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return -1
    count = 0
    for c in args.children:
        if c.type in ("(", ")", ","):
            continue
        count += 1
    return count


def _call_arg_names(src: bytes, call_node) -> list[str]:
    """Best-effort argument-name extraction for PASSES edges."""
    out: list[str] = []
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return out
    for c in args.children:
        if c.type in ("(", ")", ","):
            continue
        if c.type == "identifier":
            out.append(text(src, c))
        elif c.type == "assignment_expression":
            left = c.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                out.append(text(src, left))
    return out


def _str_lit(src: bytes, node) -> str:
    """Literal value of a Java string_literal, without quotes."""
    if node.type != "string_literal":
        return ""
    raw = text(src, node)
    return raw[1:-1] if len(raw) >= 2 and raw[0] == '"' else raw


def _annotation_nodes(node):
    for child in node.children:
        if child.type == "modifiers":
            for m in child.children:
                if m.type in ("annotation", "marker_annotation"):
                    yield m


def _annotation_value(src: bytes, ann_node) -> str:
    """The route string of a mapping annotation: a bare value or value=/path=."""
    args = ann_node.child_by_field_name("arguments")
    if args is None:
        return ""
    for c in args.children:
        if c.type == "string_literal":
            return _str_lit(src, c)
        if c.type == "element_value_pair":
            key = c.child_by_field_name("key")
            val = c.child_by_field_name("value")
            if key is not None and text(src, key) in ("value", "path") \
                    and val is not None and val.type == "string_literal":
                return _str_lit(src, val)
    return ""


def _request_mapping_method(src: bytes, ann_node) -> str:
    """@RequestMapping(method = RequestMethod.POST) -> 'POST' (default '')."""
    args = ann_node.child_by_field_name("arguments")
    if args is None:
        return ""
    for c in args.children:
        if c.type == "element_value_pair":
            key = c.child_by_field_name("key")
            val = c.child_by_field_name("value")
            if key is not None and text(src, key) == "method" and val is not None:
                return text(src, val).rsplit(".", 1)[-1].upper()
    return ""


def _request_mapping_prefix(src: bytes, type_node) -> str:
    """Class-level @RequestMapping route prefix, or '' if none."""
    for ann in _annotation_nodes(type_node):
        nm = ann.child_by_field_name("name")
        if nm is not None and simple_type_name(text(src, nm)) == "RequestMapping":
            return _annotation_value(src, ann)
    return ""


def _spring_endpoints(src: bytes, method_node, prefix: str):
    """(METHOD, route) for each Spring mapping annotation on a method."""
    out = []
    for ann in _annotation_nodes(method_node):
        nm = ann.child_by_field_name("name")
        if nm is None:
            continue
        name = simple_type_name(text(src, nm))
        if name in _SPRING_MAPPING:
            out.append((_SPRING_MAPPING[name], prefix + _annotation_value(src, ann)))
        elif name == "RequestMapping":
            verb = _request_mapping_method(src, ann) or "GET"
            out.append((verb, prefix + _annotation_value(src, ann)))
    return out


def _outbound_java(src: bytes, call_node, name: str):
    """If this invocation is a RestTemplate-style HTTP call, return (METHOD, url)."""
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return None
    first = next((c for c in args.children if c.type not in ("(", ")", ",")), None)
    if first is None or first.type != "string_literal":
        return None
    url = _str_lit(src, first)
    if not url:
        return None
    if name in _REST_TEMPLATE_CALLS:
        return _REST_TEMPLATE_CALLS[name], url
    if name == "exchange":
        # exchange(url, HttpMethod.POST, ...) -> verb from the 2nd argument
        pos = [c for c in args.children if c.type not in ("(", ")", ",")]
        verb = "GET"
        if len(pos) >= 2:
            verb = text(src, pos[1]).rsplit(".", 1)[-1].upper()
        return verb, url
    return None


def _outbound_event_java(src: bytes, call_node, name: str) -> str:
    if name not in _EVENT_EMIT_METHODS:
        return ""
    return _first_string_arg(src, call_node, 0)


def _inbound_event_java(src: bytes, call_node, name: str) -> str:
    if name not in _EVENT_CONSUME_METHODS:
        return ""
    return _first_string_arg(src, call_node, 0)


def _first_string_arg(src: bytes, call_node, index: int) -> str:
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return ""
    pos = [c for c in args.children if c.type not in ("(", ")", ",")]
    if index >= len(pos) or pos[index].type != "string_literal":
        return ""
    return _str_lit(src, pos[index]).strip()


def _java_auth_policy_specs(src: bytes, node) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for ann in _annotation_nodes(node):
        nm = ann.child_by_field_name("name")
        if nm is None:
            continue
        name = simple_type_name(text(src, nm))
        low = name.lower()
        if name in _AUTH_REQUIRE_ANNOTATIONS or "auth" in low:
            out.append(("REQUIRES_AUTH", "AUTH_REQUIRED"))
        if name in _POLICY_ANNOTATIONS or any(t in low for t in ("role", "permission", "policy", "scope", "authorize", "secured")):
            target = _annotation_policy_value(src, ann) or name or "POLICY"
            out.append(("ENFORCES_POLICY", target))
    return out


def _annotation_policy_value(src: bytes, ann_node) -> str:
    args = ann_node.child_by_field_name("arguments")
    if args is None:
        return ""
    for c in args.children:
        if c.type == "string_literal":
            return _str_lit(src, c)
        if c.type == "element_value_pair":
            val = c.child_by_field_name("value")
            if val is not None and val.type == "string_literal":
                return _str_lit(src, val)
    return ""


def _java_event_consumer_topics(src: bytes, node) -> list[str]:
    out: list[str] = []
    for ann in _annotation_nodes(node):
        nm = ann.child_by_field_name("name")
        if nm is None:
            continue
        name = simple_type_name(text(src, nm))
        if name not in _EVENT_CONSUMER_ANNOTATIONS:
            continue
        args = ann.child_by_field_name("arguments")
        if args is None:
            continue
        for c in args.children:
            if c.type == "string_literal":
                topic = _str_lit(src, c).strip()
                if topic:
                    out.append(topic)
            elif c.type == "element_value_pair":
                key = c.child_by_field_name("key")
                val = c.child_by_field_name("value")
                if key is None or val is None:
                    continue
                if text(src, key) in ("topic", "topics", "queue", "queues", "destination", "value"):
                    if val.type == "string_literal":
                        topic = _str_lit(src, val).strip()
                        if topic:
                            out.append(topic)
    dedup: list[str] = []
    for t in out:
        if t not in dedup:
            dedup.append(t)
    return dedup


def _types_in(src: bytes, node):
    """Collect simple type names under a superclass/interfaces node."""
    out = []
    for d in [node, *iter_descendants(node)]:
        if d.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
            name = simple_type_name(text(src, d))
            if name and name not in out:
                out.append(name)
    return out


def _complexity_counts(body):
    if body is None:
        return 0, 0
    branch_nodes = {
        "if_statement",
        "switch_expression",
        "switch_block_statement_group",
        "conditional_expression",
        "catch_clause",
    }
    loop_nodes = {"for_statement", "enhanced_for_statement", "while_statement", "do_statement"}
    branch_count = 0
    loop_count = 0
    for n in iter_descendants(body):
        if n.type in branch_nodes:
            branch_count += 1
        if n.type in loop_nodes:
            loop_count += 1
    return branch_count, loop_count
