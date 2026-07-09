"""Stage 1 — JavaScript/TypeScript structural extraction via tree-sitter.

Handles .js/.jsx/.mjs/.cjs (javascript grammar, JSX-aware), .ts/.mts/.cts
(typescript grammar) and .tsx (tsx grammar). One extractor for all three since
the node types overlap heavily; TypeScript adds type annotations, which drive
the RETURNS/HAS_TYPE/OF_TYPE type edges.

Emits:
    Nodes: File, Class (kind=class|interface), Function (kind=function|method),
           Field (class fields) — with range, visibility, async/static, params,
           TS return/param types, metrics (loc, cyclomatic).
    Edges (resolved): CONTAINS, DEFINES.
    RawRefs (name-only, resolved later): IMPORTS, CALLS, EXTENDS, IMPLEMENTS,
           RETURNS/HAS_TYPE/OF_TYPE (TS types).

No DFG/taint yet for JS/TS — run_dataflow only summarizes python/java, so JS
functions get the structural graph (calls, imports, types) but no dfg_json.
That is the intended first landing; JS taint is a separate step.
"""
from __future__ import annotations

import os

from ..discovery import FileInfo
from ..ids import body_hash, make_id
from ..models import Edge, Node, Origin, RawRef
from ..languages import get_parser
from .common import simple_type_name, text

EXTRACTOR = "tree-sitter"

# Class/function/field node types across the JS + TS grammars.
_CLASS_TYPES = frozenset({"class_declaration", "abstract_class_declaration"})
_FUNC_DECL_TYPES = frozenset({"function_declaration", "generator_function_declaration"})
_FUNC_EXPR_TYPES = frozenset({
    "arrow_function", "function_expression", "generator_function",
})
_METHOD_TYPES = frozenset({"method_definition"})
_FIELD_TYPES = frozenset({"public_field_definition", "field_definition"})
# Node types that open a new function/class scope — call attribution stops here.
_SCOPE_BOUNDARY = (
    _FUNC_DECL_TYPES | _FUNC_EXPR_TYPES | _METHOD_TYPES | _CLASS_TYPES
    | frozenset({"interface_declaration", "method_signature"})
)

_BRANCH_TYPES = frozenset({
    "if_statement", "switch_case", "catch_clause", "ternary_expression",
    "conditional_expression",
})
_LOOP_TYPES = frozenset({
    "for_statement", "for_in_statement", "while_statement", "do_statement",
})


def _module_fqn(relpath: str) -> str:
    no_ext = os.path.splitext(relpath)[0]
    parts = [p for p in no_ext.split(os.sep) if p and p != "index"]
    return ".".join(parts) or os.path.splitext(os.path.basename(relpath))[0]


def _visibility(name: str, node) -> str:
    """TS accessibility modifier if present; else `#`/`_` conventions; else public."""
    for c in node.children:
        if c.type == "accessibility_modifier":
            for tok in c.children:
                if tok.type in ("private", "protected", "public"):
                    return tok.type
    if name.startswith("#"):
        return "private"
    if name.startswith("_"):
        return "protected"
    return "public"


def _has_child_type(node, type_name: str) -> bool:
    return any(c.type == type_name for c in node.children)


def _name_text(src: bytes, node) -> str:
    n = node.child_by_field_name("name")
    return text(src, n) if n is not None else ""


def _member_tail(src: bytes, fn_node) -> str:
    """Callee name: identifier -> its text; member_expression a.b.c -> 'c'."""
    if fn_node is None:
        return ""
    if fn_node.type == "identifier":
        return text(src, fn_node)
    if fn_node.type == "member_expression":
        prop = fn_node.child_by_field_name("property")
        return text(src, prop) if prop is not None else ""
    if fn_node.type == "parenthesized_expression" and fn_node.named_children:
        return _member_tail(src, fn_node.named_children[0])
    return ""


def _receiver(src: bytes, fn_node) -> str:
    """Receiver tail for a member call a.b.c() -> 'a' (leftmost); '' for bare."""
    if fn_node is None or fn_node.type != "member_expression":
        return ""
    obj = fn_node.child_by_field_name("object")
    while obj is not None and obj.type == "member_expression":
        obj = obj.child_by_field_name("object")
    if obj is None:
        return ""
    if obj.type in ("identifier", "this", "super"):
        return text(src, obj)
    return ""


def _type_annotation_names(src: bytes, ann_node) -> list[str]:
    """Simple type names inside a type_annotation, primary first then generics.
    e.g. `: Promise<User>` -> ['Promise', 'User']; `: string` -> ['string']."""
    if ann_node is None:
        return []
    out: list[str] = []

    def walk(n):
        if n.type in ("type_identifier", "predefined_type"):
            nm = simple_type_name(text(src, n))
            if nm and nm not in out:
                out.append(nm)
        for c in n.named_children:
            walk(c)

    # The annotation node's payload is its named child (skip the ':' token).
    for c in ann_node.named_children:
        walk(c)
    return out


def _params(src: bytes, formal_parameters) -> tuple[list[str], list[str]]:
    """(param_names, param_types) aligned; '' type when untyped (plain JS)."""
    names: list[str] = []
    types: list[str] = []
    if formal_parameters is None:
        return names, types
    for p in formal_parameters.named_children:
        pat = p.child_by_field_name("pattern") or p
        # required_parameter/optional_parameter wrap a pattern; plain JS is bare.
        if p.type in ("required_parameter", "optional_parameter"):
            pat = p.child_by_field_name("pattern")
            ann = p.child_by_field_name("type")
        elif p.type == "identifier":
            pat, ann = p, None
        elif p.type in ("assignment_pattern",):
            pat = p.child_by_field_name("left")
            ann = None
        else:
            pat, ann = p, None
        if pat is None or pat.type not in ("identifier",):
            # destructuring / rest — record a placeholder to keep positions aligned.
            names.append(text(src, pat) if pat is not None else "")
            types.append("")
            continue
        names.append(text(src, pat))
        tnames = _type_annotation_names(src, ann)
        types.append(tnames[0] if tnames else "")
    return names, types


def _complexity_counts(body) -> tuple[int, int]:
    """(branch_count, loop_count) within a function body, not descending into
    nested function/class scopes."""
    branch = 0
    loop = 0
    stack = list(body.children) if body is not None else []
    while stack:
        n = stack.pop()
        if n.type in _SCOPE_BOUNDARY:
            continue  # nested scope — counted with its own function
        if n.type in _BRANCH_TYPES:
            branch += 1
        elif n.type in _LOOP_TYPES:
            loop += 1
        elif n.type == "binary_expression":
            op = n.child_by_field_name("operator")
            if op is not None and op.type in ("&&", "||"):
                branch += 1
        stack.extend(n.children)
    return branch, loop


def _scope_calls(body):
    """call_expression nodes in this function's own scope (not nested funcs)."""
    out = []
    if body is None:
        return out
    stack = list(body.children)
    while stack:
        n = stack.pop()
        if n.type in _SCOPE_BOUNDARY:
            continue
        if n.type == "call_expression":
            out.append(n)
        stack.extend(n.children)
    return out


def extract(file: FileInfo, repo: str):
    src = file.source
    tree = get_parser(file.lang).parse(src)
    root = tree.root_node
    lang = file.lang

    nodes: list[Node] = []
    edges: list[Edge] = []
    refs: list[RawRef] = []
    created_fields: set[str] = set()

    def contains(container_id, node, child_id):
        edges.append(Edge(
            "CONTAINS", container_id, child_id, origin=Origin.EXTRACTED.value,
            extractor=EXTRACTOR, evidence_file=file.relpath,
            evidence_line=node.start_point[0] + 1, evidence_col=node.start_point[1],
        ))

    def defines(container_id, node, child_id):
        edges.append(Edge(
            "DEFINES", container_id, child_id, origin=Origin.EXTRACTED.value,
            extractor=EXTRACTOR, evidence_file=file.relpath,
            evidence_line=node.start_point[0] + 1, evidence_col=node.start_point[1],
        ))

    def ref(rtype, src_id, target, kind_hint, node, recv="", call_arity=-1,
            import_fqn=""):
        if not target:
            return
        refs.append(RawRef(
            rtype, src_id, target, kind_hint, recv=recv, import_fqn=import_fqn,
            ref_file=file.relpath, ref_line=node.start_point[0] + 1,
            ref_col=node.start_point[1], call_arity=call_arity,
        ))

    def emit_type(rtype, src_id, ann_node):
        names = _type_annotation_names(src, ann_node)
        if not names:
            return
        ref(rtype, src_id, names[0], "type", ann_node)
        for g in names[1:]:
            ref("HAS_GENERIC", src_id, g, "type", ann_node)

    module_fqn = _module_fqn(file.relpath)
    file_id = make_id(repo, file.relpath, "file")
    file_package = os.path.dirname(file.relpath).replace(os.sep, ".")
    nodes.append(Node(
        id=file_id, label="File", name=os.path.basename(file.relpath),
        fqn=file.relpath, repo=repo, kind="file", lang=lang, file=file.relpath,
        package=file_package, start_line=1, start_col=0,
        end_line=root.end_point[0] + 1, end_col=root.end_point[1],
        body_hash=file.sha, extractor=EXTRACTOR,
    ))

    def handle_imports(node):
        source = node.child_by_field_name("source")
        module = ""
        if source is not None:
            module = text(src, source).strip("'\"`")
        # Each imported binding becomes an IMPORTS ref (name-resolved later).
        for c in node.named_children:
            if c.type == "import_clause":
                for spec in c.named_children:
                    if spec.type == "identifier":  # default import
                        ref("IMPORTS", file_id, text(src, spec), "import", spec,
                            import_fqn=module)
                    elif spec.type == "namespace_import":
                        nm = spec.named_children[-1] if spec.named_children else None
                        if nm is not None:
                            ref("IMPORTS", file_id, text(src, nm), "import", nm,
                                import_fqn=module)
                    elif spec.type == "named_imports":
                        for imp in spec.named_children:
                            if imp.type == "import_specifier":
                                nm = imp.child_by_field_name("name")
                                if nm is not None:
                                    ref("IMPORTS", file_id, text(src, nm), "import",
                                        nm, import_fqn=module)
        if module and not node.named_children:
            ref("IMPORTS", file_id, module, "import", node, import_fqn=module)

    def handle_class(node, parent_fqn, container_id, is_interface=False):
        name = _name_text(src, node)
        if not name:
            return
        fqn = f"{parent_fqn}.{name}" if parent_fqn else name
        cid = make_id(repo, fqn, "class")
        nodes.append(Node(
            id=cid, label="Class", name=name, fqn=fqn, repo=repo,
            kind="interface" if is_interface else "class", lang=lang,
            file=file.relpath,
            start_line=node.start_point[0] + 1, start_col=node.start_point[1],
            end_line=node.end_point[0] + 1, end_col=node.end_point[1],
            visibility="public",
            is_abstract=(node.type == "abstract_class_declaration"),
            body_hash=body_hash(text(src, node)), extractor=EXTRACTOR,
        ))
        contains(container_id, node, cid)
        defines(container_id, node, cid)

        # heritage: extends / implements
        heritage = None
        for c in node.children:
            if c.type == "class_heritage":
                heritage = c
                break
        if heritage is not None:
            for h in heritage.children:
                if h.type == "extends_clause":
                    for t in h.named_children:
                        if t.type in ("identifier", "type_identifier", "member_expression"):
                            ref("EXTENDS", cid, _member_tail(src, t) or text(src, t), "type", t)
                elif h.type == "implements_clause":
                    for t in h.named_children:
                        if t.type in ("type_identifier", "identifier"):
                            ref("IMPLEMENTS", cid, text(src, t), "type", t)
        elif is_interface:
            # interface X extends Y — extends clause sits inline
            for c in node.children:
                if c.type == "extends_type_clause":
                    for t in c.named_children:
                        if t.type in ("type_identifier", "identifier"):
                            ref("EXTENDS", cid, text(src, t), "type", t)

        body = node.child_by_field_name("body")
        if body is not None:
            for member in body.named_children:
                if member.type in _METHOD_TYPES or member.type == "method_signature":
                    handle_method(member, fqn, cid)
                elif member.type in _FIELD_TYPES:
                    handle_field(member, fqn, cid)

    def handle_field(node, class_fqn, class_id):
        nm = node.child_by_field_name("name")
        if nm is None:
            return
        fname = text(src, nm)
        ffqn = f"{class_fqn}.{fname}"
        fid = make_id(repo, ffqn, "field")
        if fid in created_fields:
            return
        created_fields.add(fid)
        ann = node.child_by_field_name("type")
        tnames = _type_annotation_names(src, ann)
        nodes.append(Node(
            id=fid, label="Field", name=fname, fqn=ffqn, repo=repo, kind="field",
            lang=lang, file=file.relpath,
            start_line=node.start_point[0] + 1, start_col=node.start_point[1],
            end_line=node.end_point[0] + 1, end_col=node.end_point[1],
            visibility=_visibility(fname, node), scope="class",
            is_static=_has_child_type(node, "static"),
            return_type=tnames[0] if tnames else "", extractor=EXTRACTOR,
        ))
        contains(class_id, node, fid)
        defines(class_id, node, fid)
        if ann is not None:
            emit_type("OF_TYPE", fid, ann)

    def handle_method(node, class_fqn, container_id):
        name = _name_text(src, node)
        if not name:
            return
        _emit_function(node, class_fqn, container_id, name, kind="method",
                       signature_only=(node.type == "method_signature"))

    def handle_function_decl(node, parent_fqn, container_id):
        name = _name_text(src, node)
        if not name:
            return
        _emit_function(node, parent_fqn, container_id, name, kind="function")

    def handle_var_function(declarator, parent_fqn, container_id):
        """const x = () => {} / const x = function() {} -> a Function node."""
        nm = declarator.child_by_field_name("name")
        val = declarator.child_by_field_name("value")
        if nm is None or val is None or val.type not in _FUNC_EXPR_TYPES:
            return
        if nm.type != "identifier":
            return
        _emit_function(val, parent_fqn, container_id, text(src, nm),
                       kind="function", name_node=nm)

    def _emit_function(node, parent_fqn, container_id, name, kind,
                       signature_only=False, name_node=None):
        fqn = f"{parent_fqn}.{name}" if parent_fqn else name
        mid = make_id(repo, fqn, "method" if kind == "method" else "function")
        params_node = node.child_by_field_name("parameters")
        rt_node = node.child_by_field_name("return_type")
        pnames, ptypes = _params(src, params_node)
        body = node.child_by_field_name("body")
        branch_count, loop_count = _complexity_counts(body)
        sig_params = text(src, params_node) if params_node is not None else "()"
        rt_names = _type_annotation_names(src, rt_node)
        nodes.append(Node(
            id=mid, label="Function", name=name, fqn=fqn, repo=repo, kind=kind,
            lang=lang, file=file.relpath,
            start_line=node.start_point[0] + 1, start_col=node.start_point[1],
            end_line=node.end_point[0] + 1, end_col=node.end_point[1],
            visibility=_visibility(name, node),
            is_static=_has_child_type(node, "static"),
            is_async=_has_child_type(node, "async"),
            is_abstract=_has_child_type(node, "abstract") or signature_only,
            return_type=rt_names[0] if rt_names else "",
            param_count=len(pnames), param_names=pnames, param_types=ptypes,
            signature=f"{name}{sig_params}",
            loc=(node.end_point[0] - node.start_point[0]) + 1,
            cyclomatic=1 + branch_count + loop_count,
            branch_count=branch_count, loop_count=loop_count,
            body_hash=body_hash(text(src, node)), extractor=EXTRACTOR,
        ))
        contains(container_id, node, mid)
        defines(container_id, node, mid)
        if rt_node is not None:
            emit_type("RETURNS", mid, rt_node)
        if params_node is not None:
            for p in params_node.named_children:
                ann = p.child_by_field_name("type") if p.type in (
                    "required_parameter", "optional_parameter") else None
                if ann is not None:
                    emit_type("HAS_TYPE", mid, ann)
        if body is not None and not signature_only:
            for call in _scope_calls(body):
                fn = call.child_by_field_name("function")
                callee = _member_tail(src, fn)
                if callee:
                    args = call.child_by_field_name("arguments")
                    arity = len(args.named_children) if args is not None else -1
                    ref("CALLS", mid, callee, "call", fn,
                        recv=_receiver(src, fn), call_arity=arity)
            # nested named functions/classes declared inside this body
            _walk_container(body, fqn, mid)
        return mid

    def _walk_container(container, parent_fqn, container_id):
        """Walk direct statements of a program/statement_block/class handling
        declarations. Unwraps export statements. Does not descend into function
        bodies (those recurse via _emit_function)."""
        for stmt in container.named_children:
            node = stmt
            if node.type == "export_statement":
                decl = node.child_by_field_name("declaration")
                if decl is None:
                    # re-export / export { ... } — skip (no local decl)
                    continue
                node = decl
            if node.type in _CLASS_TYPES:
                handle_class(node, parent_fqn, container_id)
            elif node.type == "interface_declaration":
                handle_class(node, parent_fqn, container_id, is_interface=True)
            elif node.type in _FUNC_DECL_TYPES:
                handle_function_decl(node, parent_fqn, container_id)
            elif node.type in ("lexical_declaration", "variable_declaration"):
                for d in node.named_children:
                    if d.type == "variable_declarator":
                        handle_var_function(d, parent_fqn, container_id)

    # top-level: imports + declarations
    for stmt in root.named_children:
        if stmt.type == "import_statement":
            handle_imports(stmt)
    _walk_container(root, module_fqn, file_id)

    return nodes, edges, refs
