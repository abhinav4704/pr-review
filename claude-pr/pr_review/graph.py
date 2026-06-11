"""Multi-language code knowledge graph using tree-sitter.

Falls back to Python ast if tree-sitter grammars are unavailable for a language.

Node kinds:
    file | function | method | class | route | table | event

Edge types (7):
    defines     file -> definition  (file contains this node)
    calls       A -> B              (A calls / invokes B)
    imports     file_a -> file_b    (intra-repo import)
    inherits    Child -> Parent     (class inheritance)
    overrides   Method -> ParentMethod (method override detected by name + class hierarchy)
    decorates   decorator_fn -> decorated_fn
    instantiates A -> Class         (A constructs an instance of Class)

Supported languages (by file extension):
    .py   Python   (tree-sitter-python + FastAPI Depends() resolution)
    .js   JavaScript  (tree-sitter-javascript)
    .ts   TypeScript  (tree-sitter-javascript used as fallback)
    .java Java     (tree-sitter-java)
    .go   Go       (tree-sitter-go)
"""

from __future__ import annotations

import ast as pyast
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx

# ── tree-sitter language loading ──────────────────────────────────────────────
def _load_languages() -> Dict[str, object]:
    langs: Dict[str, object] = {}
    try:
        from tree_sitter import Language
        import tree_sitter_python as _tspy
        langs["python"] = Language(_tspy.language())
    except Exception:
        pass
    try:
        from tree_sitter import Language
        import tree_sitter_javascript as _tsjs
        langs["javascript"] = Language(_tsjs.language())
        langs["typescript"] = langs["javascript"]
    except Exception:
        pass
    try:
        from tree_sitter import Language
        import tree_sitter_java as _tsjava
        langs["java"] = Language(_tsjava.language())
    except Exception:
        pass
    try:
        from tree_sitter import Language
        import tree_sitter_go as _tsgo
        langs["go"] = Language(_tsgo.language())
    except Exception:
        pass
    return langs


_LANGS = _load_languages()

EXT_TO_LANG = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".java": "java", ".go": "go",
}

# FastAPI / framework patterns that define HTTP routes
_ROUTE_DECORATORS = {"get", "post", "put", "patch", "delete", "head", "options",
                     "route", "api_route", "websocket"}
# SQLAlchemy/Django ORM table patterns
_TABLE_BASES = {"Base", "Model", "AbstractModel", "DeclarativeBase"}
# Celery/RQ task / event patterns
_EVENT_DECORATORS = {"task", "shared_task", "on_event", "subscribe", "listener",
                     "event_handler", "on"}
# FastAPI Depends() sentinel
_DEPENDS = "Depends"


def _is_test_path(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    return (
        os.path.basename(p).startswith("test_")
        or os.path.basename(p).endswith("_test.py")
        or "/tests/" in p or "/test/" in p or "/spec/" in p
    )


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node.text else ""


def _child_text(node, field_name: str) -> str:
    c = node.child_by_field_name(field_name)
    return _text(c) if c else ""


# ── Python extractor ──────────────────────────────────────────────────────────
class _PyExtractor:
    """Walk a Python file with tree-sitter and extract all definitions + relationships."""

    def __init__(self, path: str, src: bytes, g: nx.DiGraph,
                 by_simple: Dict[str, List[str]], module_index: Dict[str, str]) -> None:
        self.path = path
        self.src = src
        self.g = g
        self.by_simple = by_simple
        self.module_index = module_index
        self.is_test = _is_test_path(path)
        self._scope: List[str] = []  # class/function nesting
        self._depends_params: Dict[str, str] = {}  # param_name -> dependency_fn_name

    def _node_id(self, qualname: str) -> str:
        return f"{self.path}::{qualname}"

    def _add_def(self, kind: str, name: str, start: int, end: int,
                 extra: dict | None = None) -> str:
        qualname = ".".join(self._scope + [name])
        nid = self._node_id(qualname)
        attrs = dict(kind=kind, path=self.path, name=name, qualname=qualname,
                     start_line=start, end_line=end, lang="python",
                     is_test=self.is_test or name.startswith("test"))
        if extra:
            attrs.update(extra)
        self.g.add_node(nid, **attrs)
        self.g.add_edge(self.path, nid, type="defines")
        self.by_simple.setdefault(name, []).append(nid)
        return nid

    def extract(self, tree_root) -> None:
        self._visit(tree_root)

    def _visit(self, node) -> None:
        if node.type == "class_definition":
            self._handle_class(node)
        elif node.type in ("function_definition", "async_function_definition"):
            self._handle_function(node)
        elif node.type in ("import_statement", "import_from_statement"):
            self._handle_import(node)
        elif node.type in ("call",):
            self._handle_call(node)
        else:
            for child in node.children:
                self._visit(child)

    def _handle_class(self, node) -> None:
        name = _child_text(node, "name")
        if not name:
            return
        start, end = node.start_point[0] + 1, node.end_point[0] + 1

        # detect table (ORM model)
        kind = "class"
        bases = []
        for arg in node.children:
            if arg.type == "argument_list":
                for b in arg.children:
                    if b.type == "identifier":
                        bases.append(_text(b))
        if any(b in _TABLE_BASES for b in bases):
            kind = "table"

        nid = self._add_def(kind, name, start, end)

        # inheritance edges
        for base in bases:
            self.g.add_edge(nid, f"__unresolved__::{base}", type="inherits")

        self._scope.append(name)
        body = node.child_by_field_name("body")
        if body:
            for child in body.children:
                self._visit(child)
        self._scope.pop()

    def _handle_function(self, node) -> None:
        name = _child_text(node, "name")
        if not name:
            return
        start, end = node.start_point[0] + 1, node.end_point[0] + 1
        kind = "method" if self._scope else "function"

        # check decorators for route / event / task classification
        extra: dict = {}
        dec_names: List[str] = []
        route_info: dict = {}
        parent = node.parent
        if parent:
            for sib in parent.children:
                if sib.type == "decorator":
                    dec_text = _text(sib).lstrip("@")
                    dec_name = dec_text.split("(")[0].split(".")[-1]
                    dec_names.append(dec_name)
                    if dec_name in _ROUTE_DECORATORS:
                        kind = "route"
                        # extract path string if present
                        call_node = sib.child_by_field_name("expression")
                        if call_node and call_node.type == "call":
                            args = call_node.child_by_field_name("arguments")
                            if args and args.children:
                                first = next(
                                    (c for c in args.children
                                     if c.type == "string"), None
                                )
                                if first:
                                    route_info["route_path"] = _text(first).strip("'\"")
                        extra["route_method"] = dec_name.upper()
                    elif dec_name in _EVENT_DECORATORS:
                        kind = "event"
                    extra["decorators"] = dec_names
        if route_info:
            extra.update(route_info)

        # FastAPI Depends() resolution: scan parameters
        depends_params: Dict[str, str] = {}
        params_node = node.child_by_field_name("parameters")
        if params_node:
            for p in params_node.children:
                if p.type in ("typed_parameter", "default_parameter",
                               "typed_default_parameter"):
                    pname_node = p.child_by_field_name("name") or (
                        p.children[0] if p.children else None
                    )
                    pname = _text(pname_node) if pname_node else ""
                    # look for  = Depends(something)
                    for sub in p.children:
                        if sub.type == "call":
                            fn = sub.child_by_field_name("function")
                            if fn and _text(fn) == _DEPENDS:
                                args = sub.child_by_field_name("arguments")
                                if args:
                                    dep_arg = next(
                                        (c for c in args.children
                                         if c.type == "identifier"), None
                                    )
                                    if dep_arg and pname:
                                        depends_params[pname] = _text(dep_arg)

        nid = self._add_def(kind, name, start, end, extra)

        # decorator edges: each decorator fn -> this function
        for dn in dec_names:
            self.g.add_edge(f"__unresolved__::{dn}", nid, type="decorates")

        # overrides: if we're inside a class, mark override for later resolution
        if self._scope:
            parent_class = self._scope[-1]
            extra["overrides_in"] = parent_class

        # walk body to extract calls + instantiations
        self._scope.append(name)
        body = node.child_by_field_name("body")
        if body:
            self._extract_calls_in_body(body, nid, depends_params)
        self._scope.pop()

    def _extract_calls_in_body(self, body_node, caller_id: str,
                                depends_params: Dict[str, str]) -> None:
        """Extract calls, instantiations, and Depends-resolved edges."""
        for node in self._iter_all(body_node):
            if node.type == "call":
                fn = node.child_by_field_name("function")
                if not fn:
                    continue
                called = _text(fn)
                simple = called.split(".")[-1]
                # Depends parameter usage: if function is called with a depends
                # param, add edge to the dependency provider
                if simple in depends_params:
                    dep_fn = depends_params[simple]
                    self.g.add_edge(caller_id, f"__unresolved__::{dep_fn}",
                                    type="calls")
                elif simple:
                    # detect instantiation: ClassName() where name starts uppercase
                    if simple[0].isupper():
                        self.g.add_edge(caller_id, f"__unresolved__::{simple}",
                                        type="instantiates")
                    else:
                        self.g.add_edge(caller_id, f"__unresolved__::{simple}",
                                        type="calls")

    def _extract_calls_in_body_import(self, node) -> List[Tuple[str, str]]:
        return []

    def _handle_import(self, node) -> None:
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    mod = _text(child).replace(".", "/")
                    for ext in (".py", "/__init__.py"):
                        tgt = self.module_index.get(mod + ext) or \
                              self.module_index.get(_text(child))
                        if tgt:
                            self.g.add_edge(self.path, tgt, type="imports")
                            break
        elif node.type == "import_from_statement":
            mod_node = node.child_by_field_name("module_name")
            if mod_node:
                mod = _text(mod_node)
                mod_path = mod.replace(".", "/")
                tgt = self.module_index.get(mod_path + ".py") or \
                      self.module_index.get(mod) or \
                      self.module_index.get(mod_path + "/__init__.py")
                if tgt:
                    self.g.add_edge(self.path, tgt, type="imports")

    def _handle_call(self, node) -> None:
        pass  # handled in _extract_calls_in_body

    def _iter_all(self, node):
        yield node
        for child in node.children:
            yield from self._iter_all(child)


# ── Generic tree-sitter extractor (JS/Java/Go) ────────────────────────────────
class _GenericExtractor:
    """Extract definitions and calls from JS/Java/Go using tree-sitter."""

    # Per-language node-type maps
    _FUNC_TYPES = {
        "javascript": {"function_declaration", "method_definition",
                       "arrow_function", "function_expression"},
        "typescript": {"function_declaration", "method_definition",
                       "arrow_function", "function_expression"},
        "java": {"method_declaration", "constructor_declaration"},
        "go": {"function_declaration", "method_declaration"},
    }
    _CLASS_TYPES = {
        "javascript": {"class_declaration", "class_expression"},
        "typescript": {"class_declaration", "class_expression"},
        "java": {"class_declaration", "interface_declaration"},
        "go": {"type_declaration"},
    }

    def __init__(self, path: str, lang: str, src: bytes, g: nx.DiGraph,
                 by_simple: Dict[str, List[str]]) -> None:
        self.path = path
        self.lang = lang
        self.src = src
        self.g = g
        self.by_simple = by_simple
        self.is_test = _is_test_path(path)
        self._scope: List[str] = []

    def _nid(self, qualname: str) -> str:
        return f"{self.path}::{qualname}"

    def _add_def(self, kind: str, name: str, start: int, end: int) -> str:
        qualname = ".".join(self._scope + [name])
        nid = self._nid(qualname)
        self.g.add_node(nid, kind=kind, path=self.path, name=name,
                        qualname=qualname, start_line=start, end_line=end,
                        lang=self.lang, is_test=self.is_test)
        self.g.add_edge(self.path, nid, type="defines")
        self.by_simple.setdefault(name, []).append(nid)
        return nid

    def extract(self, root) -> None:
        self._walk(root)

    def _walk(self, node) -> None:
        fn_types = self._FUNC_TYPES.get(self.lang, set())
        cls_types = self._CLASS_TYPES.get(self.lang, set())

        if node.type in cls_types:
            name = self._get_name(node) or "__anon__"
            start, end = node.start_point[0] + 1, node.end_point[0] + 1
            nid = self._add_def("class", name, start, end)
            # inheritance
            self._extract_bases(node, nid)
            self._scope.append(name)
            for child in node.children:
                self._walk(child)
            self._scope.pop()

        elif node.type in fn_types:
            name = self._get_name(node) or "__anon__"
            kind = "method" if self._scope else "function"
            start, end = node.start_point[0] + 1, node.end_point[0] + 1
            nid = self._add_def(kind, name, start, end)
            self._scope.append(name)
            for child in node.children:
                self._walk(child)
            self._scope.pop()
            # extract calls in body
            body = node.child_by_field_name("body")
            if body:
                for call_node in self._iter_calls(body):
                    target = self._call_name(call_node)
                    if target:
                        edge_type = "instantiates" if target[0].isupper() else "calls"
                        self.g.add_edge(nid, f"__unresolved__::{target}",
                                        type=edge_type)
        else:
            for child in node.children:
                self._walk(child)

    def _get_name(self, node) -> Optional[str]:
        for fname in ("name", "identifier"):
            c = node.child_by_field_name(fname)
            if c:
                return _text(c)
        for child in node.children:
            if child.type == "identifier":
                return _text(child)
        return None

    def _extract_bases(self, node, nid: str) -> None:
        for sub in node.children:
            if sub.type in ("class_heritage", "superclass", "extends_clause"):
                for b in sub.children:
                    if b.type == "identifier":
                        self.g.add_edge(nid, f"__unresolved__::{_text(b)}",
                                        type="inherits")

    def _iter_calls(self, node):
        if node.type == "call_expression":
            yield node
        for child in node.children:
            yield from self._iter_calls(child)

    def _call_name(self, call_node) -> Optional[str]:
        fn = call_node.child_by_field_name("function")
        if not fn:
            return None
        if fn.type == "identifier":
            return _text(fn)
        if fn.type == "member_expression":
            prop = fn.child_by_field_name("property")
            return _text(prop) if prop else None
        return None


# ── CodeGraph dataclass ────────────────────────────────────────────────────────
@dataclass
class CodeGraph:
    g: nx.DiGraph = field(default_factory=nx.DiGraph)
    root: str = "."
    _by_simple: Dict[str, List[str]] = field(default_factory=dict)
    # language breakdown: lang -> count
    lang_counts: Dict[str, int] = field(default_factory=dict)

    def node(self, nid: str) -> dict:
        return self.g.nodes[nid]

    def has(self, nid: str) -> bool:
        return nid in self.g

    def defs_in_file(self, path: str) -> List[str]:
        return [n for n, d in self.g.nodes(data=True)
                if d.get("path") == path and d.get("kind") != "file"]

    def node_for_line(self, path: str, line: int) -> Optional[str]:
        best, best_span = None, None
        for n in self.defs_in_file(path):
            d = self.g.nodes[n]
            s, e = d.get("start_line", 0), d.get("end_line", 0)
            if s <= line <= e:
                span = e - s
                if best_span is None or span < best_span:
                    best, best_span = n, span
        return best

    def callers(self, nid: str) -> List[str]:
        return [u for u, _, t in self.g.in_edges(nid, data="type") if t == "calls"]

    def callees(self, nid: str) -> List[str]:
        return [v for _, v, t in self.g.out_edges(nid, data="type") if t == "calls"]

    def inheritors(self, nid: str) -> List[str]:
        return [u for u, _, t in self.g.in_edges(nid, data="type") if t == "inherits"]

    def decorators_of(self, nid: str) -> List[str]:
        return [u for u, _, t in self.g.in_edges(nid, data="type") if t == "decorates"]

    def fan_in(self, nid: str) -> int:
        return len(self.callers(nid))

    def routes(self) -> List[str]:
        return [n for n, d in self.g.nodes(data=True) if d.get("kind") == "route"]

    def tables(self) -> List[str]:
        return [n for n, d in self.g.nodes(data=True) if d.get("kind") == "table"]

    def events(self) -> List[str]:
        return [n for n, d in self.g.nodes(data=True) if d.get("kind") == "event"]

    def source(self, nid: str) -> str:
        d = self.g.nodes[nid]
        full = os.path.join(self.root, d["path"])
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            return ""
        return "".join(lines[d["start_line"] - 1: d["end_line"]])

    def source_lines(self, path: str, start: int, end: int) -> str:
        """Arbitrary line-range slice (for changed line context)."""
        full = os.path.join(self.root, path)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            return ""
        return "".join(lines[max(0, start - 1): end])


# ── resolve unresolved edges ───────────────────────────────────────────────────
def _resolve(g: nx.DiGraph, by_simple: Dict[str, List[str]]) -> None:
    """Replace __unresolved__ placeholder targets with real node ids."""
    to_remove: List[tuple] = []
    to_add: List[tuple] = []

    for u, v, data in list(g.edges(data=True)):
        if not str(v).startswith("__unresolved__::"):
            continue
        to_remove.append((u, v))
        name = v.split("::", 1)[1]
        candidates = by_simple.get(name, [])
        if not candidates:
            continue
        caller_file = u.split("::", 1)[0]
        # prefer same-file candidates
        same_file = [c for c in candidates if c.split("::", 1)[0] == caller_file]
        chosen = same_file or candidates
        for tgt in chosen:
            if tgt != u:
                to_add.append((u, tgt, data))

    for edge in to_remove:
        g.remove_edge(*edge)
    for u, v, d in to_add:
        g.add_edge(u, v, **d)

    # remove all unresolved placeholder nodes
    placeholders = [n for n in g.nodes if str(n).startswith("__unresolved__::")]
    g.remove_nodes_from(placeholders)


# ── overrides resolution ───────────────────────────────────────────────────────
def _resolve_overrides(g: nx.DiGraph) -> None:
    """For methods in subclasses that share a name with parent methods, add override edges."""
    # build class -> parent class edges
    class_parents: Dict[str, Set[str]] = {}
    for u, v, t in g.edges(data="type"):
        if t == "inherits":
            class_parents.setdefault(u, set()).add(v)

    # build class -> method name -> node_id
    class_methods: Dict[str, Dict[str, str]] = {}
    for n, d in g.nodes(data=True):
        if d.get("kind") in ("method", "function") and "::" in n:
            path, qualname = n.split("::", 1)
            if "." in qualname:
                cls_name, meth_name = qualname.rsplit(".", 1)
                cls_id = f"{path}::{cls_name}"
                class_methods.setdefault(cls_id, {})[meth_name] = n

    # for each class with parents, find same-named methods
    for cls_id, parents in class_parents.items():
        child_methods = class_methods.get(cls_id, {})
        for parent_id in parents:
            parent_methods = class_methods.get(parent_id, {})
            for mname, child_m in child_methods.items():
                if mname in parent_methods:
                    g.add_edge(child_m, parent_methods[mname], type="overrides")


# ── build_graph ────────────────────────────────────────────────────────────────
def build_graph(root: str, max_files: int = 5000) -> CodeGraph:
    cg = CodeGraph(root=os.path.abspath(root))
    g = cg.g

    # collect all supported source files
    source_files: List[Tuple[str, str]] = []  # (rel_path, lang)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", ".venv", "venv", "__pycache__",
                                    "node_modules", ".tox", "dist", "build",
                                    ".gradle", "target"}]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            lang = EXT_TO_LANG.get(ext)
            if lang:
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                rel = rel.replace("\\", "/")
                source_files.append((rel, lang))
        if len(source_files) >= max_files:
            break

    # module index for Python import resolution
    module_index: Dict[str, str] = {}
    for path, lang in source_files:
        if lang == "python":
            mod = path[:-3].replace("/", ".")
            module_index[mod] = path
            module_index[path] = path
            if mod.endswith(".__init__"):
                module_index[mod[: -len(".__init__")]] = path

    # add file nodes + extract definitions
    for path, lang in source_files:
        full = os.path.join(root, path)
        try:
            with open(full, "rb") as fh:
                src_bytes = fh.read()
        except OSError:
            continue

        src_text = src_bytes.decode("utf-8", errors="replace")
        line_count = src_text.count("\n") + 1

        g.add_node(path, kind="file", path=path, name=os.path.basename(path),
                   qualname="", start_line=1, end_line=line_count,
                   lang=lang, is_test=_is_test_path(path))
        cg.lang_counts[lang] = cg.lang_counts.get(lang, 0) + 1

        ts_lang = _LANGS.get(lang)
        if ts_lang:
            try:
                from tree_sitter import Parser
                parser = Parser(ts_lang)
                tree = parser.parse(src_bytes)
                if lang == "python":
                    ext = _PyExtractor(path, src_bytes, g, cg._by_simple, module_index)
                    ext.extract(tree.root_node)
                else:
                    ext2 = _GenericExtractor(path, lang, src_bytes, g, cg._by_simple)
                    ext2.extract(tree.root_node)
            except Exception:
                # fall back to ast for Python if tree-sitter parse fails
                if lang == "python":
                    _ast_fallback(path, src_text, g, cg._by_simple, module_index)
        elif lang == "python":
            _ast_fallback(path, src_text, g, cg._by_simple, module_index)

    _resolve(g, cg._by_simple)
    _resolve_overrides(g)
    return cg


# ── ast fallback for Python ────────────────────────────────────────────────────
def _ast_fallback(path: str, src: str, g: nx.DiGraph,
                  by_simple: Dict[str, List[str]],
                  module_index: Dict[str, str]) -> None:
    """Pure-ast Python extraction (used when tree-sitter is unavailable)."""
    try:
        tree = pyast.parse(src, filename=path)
    except SyntaxError:
        return
    is_test = _is_test_path(path)
    scope: List[str] = []

    def _end(node) -> int:
        e = getattr(node, "end_lineno", None)
        if e:
            return e
        last = getattr(node, "lineno", 1)
        for c in pyast.walk(node):
            cl = getattr(c, "end_lineno", None) or getattr(c, "lineno", None)
            if cl and cl > last:
                last = cl
        return last

    def visit(node):
        if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef)):
            name = node.name
            qualname = ".".join(scope + [name])
            kind = "method" if scope else "function"
            nid = f"{path}::{qualname}"
            g.add_node(nid, kind=kind, path=path, name=name, qualname=qualname,
                       start_line=node.lineno, end_line=_end(node),
                       lang="python", is_test=is_test or name.startswith("test"))
            g.add_edge(path, nid, type="defines")
            by_simple.setdefault(name, []).append(nid)
            scope.append(name)
            for child in pyast.walk(node):
                if isinstance(child, pyast.Call):
                    fn = child.func
                    called = fn.id if isinstance(fn, pyast.Name) else (
                        fn.attr if isinstance(fn, pyast.Attribute) else None
                    )
                    if called:
                        etype = "instantiates" if called[0].isupper() else "calls"
                        g.add_edge(nid, f"__unresolved__::{called}", type=etype)
            for c in node.body:
                visit(c)
            scope.pop()
        elif isinstance(node, pyast.ClassDef):
            name = node.name
            qualname = ".".join(scope + [name])
            nid = f"{path}::{qualname}"
            g.add_node(nid, kind="class", path=path, name=name, qualname=qualname,
                       start_line=node.lineno, end_line=_end(node),
                       lang="python", is_test=is_test)
            g.add_edge(path, nid, type="defines")
            by_simple.setdefault(name, []).append(nid)
            for base in node.bases:
                b = base.id if isinstance(base, pyast.Name) else None
                if b:
                    g.add_edge(nid, f"__unresolved__::{b}", type="inherits")
            scope.append(name)
            for c in node.body:
                visit(c)
            scope.pop()
        elif isinstance(node, pyast.Import):
            for alias in node.names:
                tgt = module_index.get(alias.name)
                if tgt:
                    g.add_edge(path, tgt, type="imports")
        elif isinstance(node, pyast.ImportFrom):
            if node.module:
                tgt = module_index.get(node.module)
                if tgt:
                    g.add_edge(path, tgt, type="imports")

    for n in tree.body:
        visit(n)
