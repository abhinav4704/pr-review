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

    def ref(rtype, src_id, target, kind_hint, node, recv=""):
        refs.append(RawRef(
            rtype, src_id, target, kind_hint, recv=recv,
            ref_file=file.relpath,
            ref_line=node.start_point[0] + 1, ref_col=node.start_point[1],
        ))

    module_fqn = _module_fqn(file.relpath)
    file_id = make_id(repo, file.relpath, "file")
    nodes.append(Node(
        id=file_id, label="File", name=os.path.basename(file.relpath),
        fqn=file.relpath, repo=repo, kind="file", lang="python",
        file=file.relpath, start_line=1, start_col=0,
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
            signature=f"{name}{params}", docstring=_docstring(src, node),
            body_hash=body_hash(text(src, node)), extractor=EXTRACTOR,
        ))
        contains(container_id, node, mid)
        for dname, dnode in decos:
            ref("ANNOTATED_WITH", mid, dname, "annotation", dnode)
        # type edges: return annotation + parameter annotations
        if rt_node is not None:
            _emit_type(ref, "RETURNS", mid, src, rt_node)
        if params_node is not None:
            for p in params_node.children:
                if p.type in ("typed_parameter", "typed_default_parameter"):
                    t = p.child_by_field_name("type")
                    if t is not None:
                        _emit_type(ref, "HAS_TYPE", mid, src, t)
        body = node.child_by_field_name("body")
        if body:
            # Only calls directly in this function's scope — nested defs are
            # walked separately so their calls aren't double-attributed here.
            for d in _calls_in_scope(body):
                fn = d.child_by_field_name("function")
                if fn is not None:
                    callee = _dotted_tail(src, fn)
                    if callee:
                        ref("CALLS", mid, callee, "call", fn, recv=_receiver(src, fn))
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
                    ref("IMPORTS", file_id, _dotted_tail(src, c), "import", c)
        elif child.type == "import_from_statement":
            mod = child.child_by_field_name("module_name")
            if mod is not None:
                ref("IMPORTS", file_id, _dotted_tail(src, mod), "import", mod)

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
