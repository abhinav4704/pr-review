"""Multi-language code knowledge graph using tree-sitter.

Falls back to Python ast if tree-sitter grammars are unavailable for a language.

Node kinds:
    file | function | method | class | route | table | event | field |
    variable | interface | type_alias | enum | service

Edge types:
    defines     file -> definition  (file contains this node)
    calls       A -> B              (A calls / invokes B)
    imports     file_a -> file_b    (intra-repo import)
    inherits    Child -> Parent     (class inheritance)
    overrides   Method -> ParentMethod (method override detected by name + class hierarchy)
    decorates   decorator_fn -> decorated_fn
    instantiates A -> Class         (A constructs an instance of Class)
    has_field   class -> field
    reads_field method -> field
    writes_field method -> field
    returns_type function -> type/class
    passes      caller -> callee (argument propagation)
    autowired   class -> class (DI)

Supported languages (by file extension):
    .py   Python   (tree-sitter-python + FastAPI Depends() resolution)
    .js   JavaScript  (tree-sitter-javascript)
    .ts   TypeScript  (tree-sitter-javascript used as fallback)
    .java Java     (tree-sitter-java)
    .go   Go       (tree-sitter-go)
"""

from __future__ import annotations

import ast as pyast
import json
import os
import re
from dataclasses import dataclass, field
from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx

from .graph_contract import confidence_score, edge_relation, normalize_confidence


DEFAULT_IMPACT_RELATIONS = {
    "calls",
    "instantiates",
    "overrides",
    "inherits",
    "imports",
    "references",
    "uses",
    "implements",
    "extends",
    "re_exports",
    "decorates",
    "reads_field",
    "writes_field",
    "returns_type",
    "passes",
    "autowired",
    "has_field",
}

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
_PY_CLASS_FIELD_BASES = {"BaseModel", "dataclass"}
_JAVA_ROUTE_ANNOTATIONS = {
    "GetMapping", "PostMapping", "PutMapping", "PatchMapping",
    "DeleteMapping", "RequestMapping",
}
_JAVA_SERVICE_ANNOTATIONS = {"Service", "Component", "Repository"}
_JAVA_AUTOWIRE_ANNOTATIONS = {"Autowired", "Inject"}
_JS_ROUTE_DECORATORS = {"Get", "Post", "Put", "Patch", "Delete", "Controller"}
_JS_SERVICE_DECORATORS = {"Injectable"}


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class GraphFeatureFlags:
    """Feature flags for staged, backward-compatible graph upgrades."""

    enable_language_specific_extractors: bool = False
    enable_cross_language_api_links: bool = False
    strict_language_mode: bool = False
    max_reexport_depth: int = 2

    @classmethod
    def from_env(cls) -> "GraphFeatureFlags":
        raw_depth = os.getenv("PR_REVIEW_MAX_REEXPORT_DEPTH", "2")
        try:
            depth = max(0, int(raw_depth))
        except ValueError:
            depth = 2
        return cls(
            enable_language_specific_extractors=_env_bool(
                "PR_REVIEW_ENABLE_LANGUAGE_EXTRACTORS", False
            ),
            enable_cross_language_api_links=_env_bool(
                "PR_REVIEW_ENABLE_CROSS_LANGUAGE_API_LINKS", False
            ),
            strict_language_mode=_env_bool("PR_REVIEW_STRICT_LANGUAGE_MODE", False),
            max_reexport_depth=depth,
        )


def _is_test_path(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    return (
        os.path.basename(p).startswith("test_")
        or os.path.basename(p).endswith("_test.py")
        or "/tests/" in p or "/test/" in p or "/spec/" in p
    )


def _line_from_source_location(value: object) -> int:
    """Extract the first line number from source_location values like L12-L18."""
    if value is None:
        return 0
    s = str(value)
    digits = ""
    for ch in s:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return int(digits) if digits else 0


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node.text else ""


def _child_text(node, field_name: str) -> str:
    c = node.child_by_field_name(field_name)
    return _text(c) if c else ""


def _simple_name(value: str) -> str:
    if not value:
        return ""
    return value.split(".")[-1].split("::")[-1].strip()


def _java_package_from_source(src_text: str) -> str:
    m = re.search(r"\bpackage\s+([a-zA-Z_][\w\.]*?)\s*;", src_text)
    return m.group(1) if m else ""


def _strip_json_comments(raw: str) -> str:
    """Remove // and /* */ comments from tsconfig-style JSON content."""
    # This is intentionally conservative and sufficient for tsconfig parsing.
    no_block = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
    return re.sub(r"(^|\s)//.*$", "", no_block, flags=re.M)


def _load_tsconfig_aliases(root: str) -> Tuple[str, Dict[str, List[str]]]:
    """Return (base_dir, paths map) from root tsconfig, or empty defaults."""
    tsconfig_path = os.path.join(root, "tsconfig.json")
    if not os.path.isfile(tsconfig_path):
        return "", {}
    try:
        with open(tsconfig_path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        data = json.loads(_strip_json_comments(raw))
        compiler = data.get("compilerOptions", {}) if isinstance(data, dict) else {}
        base_url = compiler.get("baseUrl", ".") if isinstance(compiler, dict) else "."
        paths = compiler.get("paths", {}) if isinstance(compiler, dict) else {}
        if not isinstance(paths, dict):
            return "", {}
        clean_paths: Dict[str, List[str]] = {}
        for k, v in paths.items():
            if not isinstance(k, str):
                continue
            vals = [x for x in v] if isinstance(v, list) else [v]
            clean_vals = [str(x) for x in vals if isinstance(x, str)]
            if clean_vals:
                clean_paths[k] = clean_vals
        base_dir = os.path.normpath(os.path.join(os.path.dirname(tsconfig_path), str(base_url))).replace("\\", "/")
        return base_dir, clean_paths
    except Exception:
        return "", {}


def _resolve_import_candidate(candidate: str, source_index: Set[str]) -> str:
    """Resolve JS/TS import candidate to an indexed file path."""
    if candidate.endswith((".js", ".jsx", ".ts", ".tsx")):
        return candidate if candidate in source_index else ""
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        with_ext = candidate + ext
        if with_ext in source_index:
            return with_ext
    for ext in ("/index.ts", "/index.tsx", "/index.js", "/index.jsx"):
        idx = candidate + ext
        if idx in source_index:
            return idx
    return ""


def _extract_js_import_locals(stmt_text: str) -> List[str]:
    """Extract local binding names from a JS/TS import declaration text."""
    m = re.match(r"\s*import\s+(.*?)\s+from\s+['\"]", stmt_text, flags=re.S)
    if not m:
        return []
    spec = m.group(1).strip()
    out: List[str] = []

    def _push_identifier(token: str) -> None:
        ident = token.strip()
        if ident and re.match(r"^[A-Za-z_$][\w$]*$", ident):
            out.append(ident)

    brace_match = re.search(r"\{(.*?)\}", spec, flags=re.S)
    if brace_match:
        inside = brace_match.group(1)
        for part in inside.split(","):
            token = part.strip()
            if not token:
                continue
            if " as " in token:
                _push_identifier(token.split(" as ", 1)[1])
            else:
                _push_identifier(token)
        spec = re.sub(r"\{.*?\}", "", spec, flags=re.S).strip().strip(",")

    ns_match = re.search(r"\*\s+as\s+([A-Za-z_$][\w$]*)", spec)
    if ns_match:
        _push_identifier(ns_match.group(1))
        spec = re.sub(r"\*\s+as\s+[A-Za-z_$][\w$]*", "", spec).strip().strip(",")

    if spec:
        _push_identifier(spec.split(",", 1)[0].strip())

    return out


# ── Python extractor ──────────────────────────────────────────────────────────
class _PyExtractor:
    """Walk a Python file with tree-sitter and extract all definitions + relationships."""

    def __init__(self, path: str, src: bytes, g: nx.DiGraph,
                 by_simple: Dict[str, List[str]], module_index: Dict[str, str],
                 bindings: Dict[str, str] | None = None) -> None:
        self.path = path
        self.src = src
        self.g = g
        self.by_simple = by_simple
        self.module_index = module_index
        # local-name -> target file for `from mod import name [as local]`, used to
        # disambiguate cross-file calls during _resolve.
        self.bindings = bindings if bindings is not None else {}
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
        elif node.type in ("assignment", "annotated_assignment") and not self._scope:
            self._handle_module_variable(node)
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
                        bases.append(_simple_name(_text(b)))
                    elif b.type == "attribute":
                        bases.append(_simple_name(_text(b)))
        if any(b in _TABLE_BASES for b in bases):
            kind = "table"

        nid = self._add_def(kind, name, start, end)

        # inheritance edges
        for base in bases:
            self.g.add_edge(nid, f"__unresolved__::{base}", type="inherits")

        self._scope.append(name)
        body = node.child_by_field_name("body")
        if body:
            if kind == "table" or any(b in _PY_CLASS_FIELD_BASES for b in bases):
                self._extract_class_fields(body, nid)
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

        return_type = node.child_by_field_name("return_type")
        return_name = _simple_name(_text(return_type))
        if return_name:
            self.g.add_edge(nid, f"__unresolved__::{return_name}", type="returns_type")

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
            self._extract_returns_in_body(body, nid)
        self._scope.pop()

    def _extract_calls_in_body(self, body_node, caller_id: str,
                                depends_params: Dict[str, str]) -> None:
        """Extract calls, instantiations, and Depends-resolved edges."""
        current_class = self._scope[-2] if len(self._scope) >= 2 else ""
        for node in self._iter_all(body_node):
            if node.type == "call":
                fn = node.child_by_field_name("function")
                if not fn:
                    continue
                called = _text(fn)
                simple = called.split(".")[-1]
                args = self._call_args(node)
                # Depends parameter usage: if function is called with a depends
                # param, add edge to the dependency provider
                if simple in depends_params:
                    dep_fn = depends_params[simple]
                    self.g.add_edge(caller_id, f"__unresolved__::{dep_fn}",
                                    type="calls")
                    if args:
                        self.g.add_edge(caller_id, f"__unresolved__::{dep_fn}",
                                        type="passes", args=args)
                elif simple:
                    # detect instantiation: ClassName() where name starts uppercase
                    if simple[0].isupper():
                        self.g.add_edge(caller_id, f"__unresolved__::{simple}",
                                        type="instantiates")
                    else:
                        self.g.add_edge(caller_id, f"__unresolved__::{simple}",
                                        type="calls")
                    if args:
                        self.g.add_edge(caller_id, f"__unresolved__::{simple}",
                                        type="passes", args=args)
            elif node.type == "attribute" and current_class:
                obj = node.child_by_field_name("object")
                attr = node.child_by_field_name("attribute")
                if _text(obj) != "self" or not attr:
                    continue
                field_target = f"__unresolved__::{current_class}.{_text(attr)}"
                parent = node.parent
                is_write = bool(
                    parent and parent.type in ("assignment", "annotated_assignment", "augmented_assignment")
                    and parent.child_by_field_name("left") is node
                )
                edge_type = "writes_field" if is_write else "reads_field"
                self.g.add_edge(caller_id, field_target, type=edge_type)

    def _extract_returns_in_body(self, body_node, caller_id: str) -> None:
        for node in self._iter_all(body_node):
            if node.type != "return_statement":
                continue
            value = node.child_by_field_name("value")
            if not value:
                continue
            if value.type == "call":
                fn = value.child_by_field_name("function")
                target = _simple_name(_text(fn)) if fn else ""
            elif value.type in ("identifier", "attribute"):
                target = _simple_name(_text(value))
            else:
                target = ""
            if target and target[0].isupper():
                self.g.add_edge(caller_id, f"__unresolved__::{target}", type="returns_type")

    def _extract_class_fields(self, body_node, class_id: str) -> None:
        class_data = self.g.nodes.get(class_id, {})
        class_name = class_data.get("name") or class_data.get("qualname") or ""
        for node in body_node.children:
            if node.type not in ("assignment", "annotated_assignment"):
                continue
            left = node.child_by_field_name("left")
            if not left or left.type != "identifier":
                continue
            field_name = _text(left)
            if not field_name:
                continue
            start, end = node.start_point[0] + 1, node.end_point[0] + 1
            self._scope.append(str(class_name))
            try:
                field_id = self._add_def("field", field_name, start, end)
            finally:
                self._scope.pop()
            self.g.add_edge(class_id, field_id, type="has_field")

    def _handle_module_variable(self, node) -> None:
        left = node.child_by_field_name("left")
        if not left or left.type != "identifier":
            return
        name = _text(left)
        if not name:
            return
        start, end = node.start_point[0] + 1, node.end_point[0] + 1
        self._add_def("variable", name, start, end)

    def _call_args(self, call_node) -> List[str]:
        out: List[str] = []
        args = call_node.child_by_field_name("arguments")
        if not args:
            return out
        for child in args.children:
            if child.type == "identifier":
                out.append(_text(child))
            elif child.type == "keyword_argument":
                k = child.child_by_field_name("name")
                if k:
                    out.append(_text(k))
        return out

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
                    # bind each imported symbol to the target file
                    seen_import_kw = False
                    for child in node.children:
                        if child.type == "import":
                            seen_import_kw = True
                            continue
                        if not seen_import_kw:
                            continue
                        if child.type in ("dotted_name", "identifier"):
                            nm = _text(child).split(".")[0]
                            if nm:
                                self.bindings[nm] = tgt
                        elif child.type == "aliased_import":
                            alias = child.child_by_field_name("alias")
                            name = child.child_by_field_name("name")
                            local = _text(alias) if alias else (_text(name) if name else "")
                            local = local.split(".")[0]
                            if local:
                                self.bindings[local] = tgt

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
    _INTERFACE_TYPES = {"typescript": {"interface_declaration"}, "java": {"interface_declaration"}}
    _TYPE_ALIAS_TYPES = {"typescript": {"type_alias_declaration"}}
    _ENUM_TYPES = {"typescript": {"enum_declaration"}, "java": {"enum_declaration"}}

    def __init__(self, path: str, lang: str, src: bytes, g: nx.DiGraph,
                 by_simple: Dict[str, List[str]],
                 bindings: Dict[str, str] | None = None,
                 package_index: Dict[str, List[str]] | None = None,
                 source_index: Set[str] | None = None,
                 java_package_by_file: Dict[str, str] | None = None,
                 java_package_classes: Dict[str, Dict[str, List[str]]] | None = None,
                 java_fqcn_index: Dict[str, str] | None = None,
                 project_root: str = "",
                 tsconfig_base_dir: str = "",
                 tsconfig_paths: Dict[str, List[str]] | None = None) -> None:
        self.path = path
        self.lang = lang
        self.src = src
        self.g = g
        self.by_simple = by_simple
        self.bindings = bindings if bindings is not None else {}
        self.package_index = package_index if package_index is not None else {}
        self.source_index = source_index if source_index is not None else set()
        self.java_package_by_file = java_package_by_file if java_package_by_file is not None else {}
        self.java_package_classes = java_package_classes if java_package_classes is not None else {}
        self.java_fqcn_index = java_fqcn_index if java_fqcn_index is not None else {}
        self.project_root = project_root.replace("\\", "/")
        self.tsconfig_base_dir = tsconfig_base_dir.replace("\\", "/")
        self.tsconfig_paths = tsconfig_paths if tsconfig_paths is not None else {}
        self.java_package = self.java_package_by_file.get(path, "") if lang == "java" else ""
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
        self._collect_import_bindings(root)
        self._walk(root)

    def _walk(self, node) -> None:
        fn_types = self._FUNC_TYPES.get(self.lang, set())
        cls_types = self._CLASS_TYPES.get(self.lang, set())
        interface_types = self._INTERFACE_TYPES.get(self.lang, set())
        type_alias_types = self._TYPE_ALIAS_TYPES.get(self.lang, set())
        enum_types = self._ENUM_TYPES.get(self.lang, set())

        if node.type in interface_types:
            name = self._get_name(node) or "__anon__"
            start, end = node.start_point[0] + 1, node.end_point[0] + 1
            self._add_def("interface", name, start, end)
            for child in node.children:
                self._walk(child)
            return

        if node.type in type_alias_types:
            name = self._get_name(node) or "__anon__"
            start, end = node.start_point[0] + 1, node.end_point[0] + 1
            self._add_def("type_alias", name, start, end)
            for child in node.children:
                self._walk(child)
            return

        if node.type in enum_types:
            name = self._get_name(node) or "__anon__"
            start, end = node.start_point[0] + 1, node.end_point[0] + 1
            self._add_def("enum", name, start, end)
            for child in node.children:
                self._walk(child)
            return

        if node.type in cls_types:
            name = self._get_name(node) or "__anon__"
            start, end = node.start_point[0] + 1, node.end_point[0] + 1
            kind = self._class_kind(node)
            nid = self._add_def(kind, name, start, end)
            # inheritance
            self._extract_bases(node, nid)
            self._extract_java_di(node, nid)
            self._scope.append(name)
            for child in node.children:
                self._walk(child)
            self._scope.pop()

        elif node.type in fn_types:
            name = self._get_name(node) or "__anon__"
            kind = self._function_kind(node)
            start, end = node.start_point[0] + 1, node.end_point[0] + 1
            nid = self._add_def(kind, name, start, end)
            self._extract_return_type(node, nid)
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
                        args = self._call_args(call_node)
                        if args:
                            self.g.add_edge(nid, f"__unresolved__::{target}",
                                            type="passes", args=args)
                self._extract_field_access(body, nid)
        else:
            for child in node.children:
                self._walk(child)

    def _collect_import_bindings(self, root) -> None:
        if self.lang == "java" and self.java_package:
            # Same-package classes are visible without imports.
            for simple, targets in self.java_package_classes.get(self.java_package, {}).items():
                if targets and simple not in self.bindings:
                    self.bindings[simple] = targets[0]
        for node in root.children:
            if node.type in (
                "import_statement",
                "import_declaration",
                "export_statement",
            ):
                self._handle_import_node(node)

    def _handle_import_node(self, node) -> None:
        if self.lang in ("javascript", "typescript"):
            source = node.child_by_field_name("source")
            source_text = _text(source).strip("'\"") if source else ""
            if not source_text:
                return
            target = self._resolve_import_path(source_text)
            if target:
                self.g.add_edge(self.path, target, type="imports")
            stmt = _text(node)
            if node.type in {"import_declaration", "import_statement"} and target:
                for local in _extract_js_import_locals(stmt):
                    self.bindings[local] = target
            if stmt.lstrip().startswith("export") and target:
                self.g.add_edge(self.path, target, type="re_exports")
        elif self.lang == "java" and node.type == "import_declaration":
            txt = _text(node).replace("import", "").replace(";", "").strip()
            if txt.startswith("static "):
                txt = txt[len("static "):].strip()
            if not txt:
                return
            if txt.endswith(".*"):
                pkg = txt[:-2]
                for simple, targets in self.java_package_classes.get(pkg, {}).items():
                    if not targets:
                        continue
                    if simple not in self.bindings:
                        self.bindings[simple] = targets[0]
                    for tgt in targets:
                        self.g.add_edge(self.path, tgt, type="imports")
                return
            simple = _simple_name(txt)
            target = self.java_fqcn_index.get(txt)
            if not target:
                targets = self.package_index.get(simple, [])
                target = targets[0] if targets else ""
            if target:
                self.bindings[simple] = target
                self.g.add_edge(self.path, target, type="imports")

    def _class_kind(self, node) -> str:
        if self.lang == "java":
            if node.type == "interface_declaration":
                return "interface"
            for ann in self._decorator_names(node):
                if ann in _JAVA_SERVICE_ANNOTATIONS:
                    return "service"
                if ann in {"RestController", "Controller"}:
                    return "route"
        if self.lang in ("javascript", "typescript"):
            for dec in self._decorator_names(node):
                if dec in _JS_SERVICE_DECORATORS:
                    return "service"
                if dec in _JS_ROUTE_DECORATORS:
                    return "route"
        return "class"

    def _function_kind(self, node) -> str:
        kind = "method" if self._scope else "function"
        if self.lang == "java":
            for ann in self._decorator_names(node):
                if ann in _JAVA_ROUTE_ANNOTATIONS:
                    return "route"
        if self.lang in ("javascript", "typescript"):
            for dec in self._decorator_names(node):
                if dec in _JS_ROUTE_DECORATORS:
                    return "route"
        return kind

    def _extract_return_type(self, node, nid: str) -> None:
        type_node = node.child_by_field_name("return_type") or node.child_by_field_name("type")
        target = _simple_name(_text(type_node)) if type_node else ""
        if target and target != "void":
            self.g.add_edge(nid, f"__unresolved__::{target}", type="returns_type")

    def _extract_java_di(self, node, class_id: str) -> None:
        if self.lang != "java":
            return
        class_body = None
        for ch in node.children:
            if ch.type == "class_body":
                class_body = ch
                break
        members = class_body.children if class_body else node.children

        constructors = [c for c in members if c.type == "constructor_declaration"]
        for child in members:
            if child.type != "field_declaration":
                continue
            annotations = set(self._decorator_names(child))
            if not annotations.intersection(_JAVA_AUTOWIRE_ANNOTATIONS):
                continue
            type_node = child.child_by_field_name("type")
            target = _simple_name(_text(type_node)) if type_node else ""
            if target:
                self.g.add_edge(class_id, f"__unresolved__::{target}", type="autowired")

        for ctor in constructors:
            ctor_annotations = set(self._decorator_names(ctor))
            infer_ctor = bool(ctor_annotations.intersection(_JAVA_AUTOWIRE_ANNOTATIONS))
            if not infer_ctor and len(constructors) != 1:
                continue
            params = ctor.child_by_field_name("parameters")
            if not params:
                continue
            for p in params.children:
                if p.type not in {"formal_parameter", "receiver_parameter", "spread_parameter"}:
                    continue
                tnode = p.child_by_field_name("type")
                target = _simple_name(_text(tnode)) if tnode else ""
                if target:
                    self.g.add_edge(class_id, f"__unresolved__::{target}", type="autowired")

    def _extract_field_access(self, body, nid: str) -> None:
        for node in self._iter_all(body):
            if node.type != "member_expression":
                continue
            obj = node.child_by_field_name("object")
            prop = node.child_by_field_name("property")
            if not obj or not prop:
                continue
            obj_text = _text(obj)
            if obj_text not in {"this", "self"}:
                continue
            field_name = _text(prop)
            if not field_name:
                continue
            class_name = self._scope[-1] if self._scope else ""
            if not class_name:
                continue
            parent = node.parent
            is_write = bool(
                parent and parent.type in ("assignment_expression", "assignment")
                and parent.child_by_field_name("left") is node
            )
            edge_type = "writes_field" if is_write else "reads_field"
            self.g.add_edge(nid, f"__unresolved__::{class_name}.{field_name}", type=edge_type)

    def _decorator_names(self, node) -> List[str]:
        out: List[str] = []
        for child in node.children:
            if child.type in ("decorator", "marker_annotation", "annotation"):
                text = _text(child).lstrip("@")
                out.append(_simple_name(text.split("(")[0]))
        return out

    def _call_args(self, call_node) -> List[str]:
        out: List[str] = []
        args = call_node.child_by_field_name("arguments")
        if not args:
            return out
        for child in args.children:
            if child.type == "identifier":
                out.append(_text(child))
            elif child.type in ("assignment_expression", "pair"):
                left = child.child_by_field_name("left") or child.child_by_field_name("key")
                if left:
                    out.append(_text(left))
        return out

    def _resolve_import_path(self, source_path: str) -> str:
        if source_path.startswith("."):
            return self._resolve_relative_path(source_path)
        return self._resolve_ts_alias_path(source_path)

    def _resolve_relative_path(self, source_path: str) -> str:
        base_dir = os.path.dirname(self.path).replace("\\", "/")
        candidate = os.path.normpath(os.path.join(base_dir, source_path)).replace("\\", "/")
        return _resolve_import_candidate(candidate, self.source_index)

    def _resolve_ts_alias_path(self, source_path: str) -> str:
        if not self.tsconfig_paths or not self.tsconfig_base_dir or not self.project_root:
            return ""
        for alias, targets in self.tsconfig_paths.items():
            if "*" in alias:
                prefix, suffix = alias.split("*", 1)
                if not (source_path.startswith(prefix) and source_path.endswith(suffix)):
                    continue
                captured = source_path[len(prefix): len(source_path) - len(suffix) if suffix else None]
                for tgt in targets:
                    replaced = tgt.replace("*", captured)
                    abs_candidate = os.path.normpath(os.path.join(self.tsconfig_base_dir, replaced)).replace("\\", "/")
                    rel_candidate = os.path.relpath(abs_candidate, self.project_root).replace("\\", "/")
                    hit = _resolve_import_candidate(rel_candidate, self.source_index)
                    if hit:
                        return hit
            elif alias == source_path:
                for tgt in targets:
                    abs_candidate = os.path.normpath(os.path.join(self.tsconfig_base_dir, tgt)).replace("\\", "/")
                    rel_candidate = os.path.relpath(abs_candidate, self.project_root).replace("\\", "/")
                    hit = _resolve_import_candidate(rel_candidate, self.source_index)
                    if hit:
                        return hit
        return ""

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
            elif sub.type in ("implements_clause", "super_interfaces"):
                for b in sub.children:
                    if b.type == "identifier":
                        self.g.add_edge(nid, f"__unresolved__::{_text(b)}",
                                        type="implements")

    def _iter_calls(self, node):
        if node.type in ("call_expression", "method_invocation", "object_creation_expression"):
            yield node
        for child in node.children:
            yield from self._iter_calls(child)

    def _call_name(self, call_node) -> Optional[str]:
        if call_node.type == "object_creation_expression":
            tnode = call_node.child_by_field_name("type")
            return _simple_name(_text(tnode)) if tnode else None
        if call_node.type == "method_invocation":
            n = call_node.child_by_field_name("name")
            if n:
                return _text(n)
            txt = _text(call_node)
            if "(" in txt:
                return _simple_name(txt.split("(", 1)[0])
            return None
        fn = call_node.child_by_field_name("function")
        if not fn:
            return None
        if fn.type == "identifier":
            return _text(fn)
        if fn.type == "member_expression":
            obj = fn.child_by_field_name("object")
            prop = fn.child_by_field_name("property")
            if obj and obj.type == "identifier" and prop:
                return f"{_text(obj)}.{_text(prop)}"
            txt = _text(fn).strip()
            m = re.match(r"^([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)$", txt)
            if m:
                return f"{m.group(1)}.{m.group(2)}"
            return _text(prop) if prop else None
        return None

    def _iter_all(self, node):
        """Depth-first traversal over all descendants (including ``node`` itself)."""
        yield node
        for child in node.children:
            yield from self._iter_all(child)


class _JavaExtractor(_GenericExtractor):
    """Java-specialized extractor with fixes for Java AST node types."""

    # ── fix 1: preserve object qualifier in method calls ─────────────────────
    def _call_name(self, call_node) -> Optional[str]:
        if call_node.type == "object_creation_expression":
            tnode = call_node.child_by_field_name("type")
            return _simple_name(_text(tnode)) if tnode else None
        if call_node.type == "method_invocation":
            name_node = call_node.child_by_field_name("name")
            if not name_node:
                txt = _text(call_node)
                return _simple_name(txt.split("(", 1)[0]) if "(" in txt else None
            method_name = _text(name_node)
            obj_node = call_node.child_by_field_name("object")
            if obj_node and obj_node.type == "identifier":
                obj_text = _text(obj_node)
                # Return "Class.method" only when the receiver is a bound class
                # reference (in same-package pre-populated bindings or an explicit
                # import).  This lets _resolve pin the right class via
                # import_bindings.  Exclude "this"/"super" and lowercase field
                # variables — those fall back to simple-name lookup, which is
                # unchanged from the _GenericExtractor behaviour.
                if (obj_text not in {"this", "super"}
                        and (obj_text in self.bindings
                             or (obj_text and obj_text[0].isupper()))):
                    return f"{obj_text}.{method_name}"
            return method_name
        # JS-style call_expression fallback (not normally reached for Java)
        fn = call_node.child_by_field_name("function")
        if not fn:
            return None
        if fn.type == "identifier":
            return _text(fn)
        if fn.type == "member_expression":
            obj = fn.child_by_field_name("object")
            prop = fn.child_by_field_name("property")
            if obj and obj.type == "identifier" and prop:
                return f"{_text(obj)}.{_text(prop)}"
            txt = _text(fn).strip()
            m = re.match(r"^([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)$", txt)
            if m:
                return f"{m.group(1)}.{m.group(2)}"
            return _text(prop) if prop else None
        return None

    # ── fix 2: use Java field_access nodes instead of JS member_expression ───
    def _extract_field_access(self, body, nid: str) -> None:
        for node in self._iter_all(body):
            if node.type != "field_access":
                continue
            obj = node.child_by_field_name("object")
            if not obj or _text(obj) != "this":
                continue
            field_node = node.child_by_field_name("field")
            if not field_node:
                # tree-sitter-java sometimes puts the field name as the last
                # identifier child rather than in a named "field" slot.
                field_node = next(
                    (c for c in reversed(node.children) if c.type == "identifier"),
                    None,
                )
            if not field_node:
                continue
            field_name = _text(field_node)
            if not field_name:
                continue
            class_name = self._scope[-1] if self._scope else ""
            if not class_name:
                continue
            parent = node.parent
            # In Java AST an assignment is "assignment_expression" with
            # left_hand_side for the lvalue (not "left" as in JS).
            is_write = bool(
                parent and parent.type == "assignment_expression"
                and (
                    parent.child_by_field_name("left_hand_side") is node
                    or parent.child_by_field_name("left") is node
                )
            )
            edge_type = "writes_field" if is_write else "reads_field"
            self.g.add_edge(nid, f"__unresolved__::{class_name}.{field_name}", type=edge_type)

    # ── fix 3: emit has_field edges for all declared Java fields ─────────────
    def _extract_java_class_fields(self, class_node, class_id: str) -> None:
        """Emit has_field + field nodes for every field_declaration in the class."""
        class_body = None
        for ch in class_node.children:
            if ch.type == "class_body":
                class_body = ch
                break
        if class_body is None:
            return
        class_data = self.g.nodes.get(class_id, {})
        class_name = class_data.get("name") or class_data.get("qualname") or ""
        for child in class_body.children:
            if child.type != "field_declaration":
                continue
            # A field_declaration may declare multiple variables:
            # "private String name, email;"
            for sub in child.children:
                if sub.type != "variable_declarator":
                    continue
                fname_node = sub.child_by_field_name("name")
                if not fname_node:
                    fname_node = next(
                        (c for c in sub.children if c.type == "identifier"), None
                    )
                if not fname_node:
                    continue
                field_name = _text(fname_node)
                if not field_name:
                    continue
                start = child.start_point[0] + 1
                end = child.end_point[0] + 1
                self._scope.append(str(class_name))
                try:
                    field_id = self._add_def("field", field_name, start, end)
                finally:
                    self._scope.pop()
                self.g.add_edge(class_id, field_id, type="has_field")

    def _walk(self, node) -> None:
        # Intercept class declarations to also extract field nodes.
        cls_types = self._CLASS_TYPES.get(self.lang, set())
        if node.type in cls_types:
            name = self._get_name(node) or "__anon__"
            start, end = node.start_point[0] + 1, node.end_point[0] + 1
            kind = self._class_kind(node)
            nid = self._add_def(kind, name, start, end)
            self._extract_bases(node, nid)
            self._extract_java_di(node, nid)
            self._extract_java_class_fields(node, nid)
            self._scope.append(name)
            for child in node.children:
                self._walk(child)
            self._scope.pop()
        else:
            super()._walk(node)


class _JsTsExtractor(_GenericExtractor):
    """JS/TS-specialized extractor entrypoint (keeps current behavior for now)."""


class _GoExtractor(_GenericExtractor):
    """Go-specialized extractor entrypoint (keeps current behavior for now)."""


def _extractor_cls_for_language(
    lang: str,
    flags: GraphFeatureFlags,
):
    if not flags.enable_language_specific_extractors:
        return _GenericExtractor
    if lang == "java":
        return _JavaExtractor
    if lang in {"javascript", "typescript"}:
        return _JsTsExtractor
    if lang == "go":
        return _GoExtractor
    return _GenericExtractor


def _safe_extract_non_python(
    extractor_cls,
    path: str,
    lang: str,
    src_bytes: bytes,
    g: nx.DiGraph,
    by_simple: Dict[str, List[str]],
    tree_root,
    import_bindings: Dict[str, Dict[str, str]],
    package_index: Dict[str, List[str]],
    source_index: Set[str],
    java_package_by_file: Dict[str, str],
    java_package_classes: Dict[str, Dict[str, List[str]]],
    java_fqcn_index: Dict[str, str],
    project_root: str,
    tsconfig_base_dir: str,
    tsconfig_paths: Dict[str, List[str]],
    flags: GraphFeatureFlags,
) -> None:
    bindings = import_bindings.setdefault(path, {})

    def _make(cls_obj):
        return cls_obj(
            path,
            lang,
            src_bytes,
            g,
            by_simple,
            bindings=bindings,
            package_index=package_index,
            source_index=source_index,
            java_package_by_file=java_package_by_file,
            java_package_classes=java_package_classes,
            java_fqcn_index=java_fqcn_index,
            project_root=project_root,
            tsconfig_base_dir=tsconfig_base_dir,
            tsconfig_paths=tsconfig_paths,
        )

    try:
        _make(extractor_cls).extract(tree_root)
    except Exception:
        if flags.strict_language_mode or extractor_cls is _GenericExtractor:
            raise
        _make(_GenericExtractor).extract(tree_root)


# ── CodeGraph dataclass ────────────────────────────────────────────────────────
@dataclass
class CodeGraph:
    g: nx.DiGraph = field(default_factory=nx.DiGraph)
    root: str = "."
    _by_simple: Dict[str, List[str]] = field(default_factory=dict)
    # language breakdown: lang -> count
    lang_counts: Dict[str, int] = field(default_factory=dict)
    # Lazily built index: path -> [(start_line, end_line, node_id)]
    _line_index: Dict[str, List[Tuple[int, int, str]]] = field(default_factory=dict)

    def node(self, nid: str) -> dict:
        return self.g.nodes[nid]

    def has(self, nid: str) -> bool:
        return nid in self.g

    def defs_in_file(self, path: str) -> List[str]:
        return [n for n, d in self.g.nodes(data=True)
                if d.get("path") == path and d.get("kind") != "file"]

    def _ensure_line_index(self, path: str) -> None:
        if path in self._line_index:
            return
        entries: List[Tuple[int, int, str]] = []
        for n, d in self.g.nodes(data=True):
            if d.get("path") == path and d.get("kind") != "file":
                s, e = d.get("start_line", 0), d.get("end_line", 0)
                if s and e:
                    entries.append((s, e, n))
        self._line_index[path] = entries

    def node_for_line(self, path: str, line: int) -> Optional[str]:
        self._ensure_line_index(path)
        best, best_span = None, None
        for s, e, n in self._line_index[path]:
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

    def reverse_dependents(self, nid: str, depth: int = 2,
                           relations: Optional[Set[str]] = None,
                           exclude_ambiguous: bool = False) -> Dict[str, int]:
        """Return reverse dependency frontier with hop distance.

        Walks incoming edges whose canonical relation belongs to relations.
        """
        relation_set = relations or DEFAULT_IMPACT_RELATIONS
        seen: Dict[str, int] = {}
        queue: deque[Tuple[str, int]] = deque([(nid, 0)])
        visited = {nid}

        while queue:
            current, d = queue.popleft()
            if d >= depth:
                continue
            for src, _tgt, data in self.g.in_edges(current, data=True):
                relation = edge_relation(data)
                if relation not in relation_set:
                    continue
                if exclude_ambiguous and str(data.get("confidence", "")).lower() == "ambiguous":
                    continue
                if src in visited:
                    continue
                visited.add(src)
                next_depth = d + 1
                seen[src] = next_depth
                queue.append((src, next_depth))

        return seen

    def caller_evidence_lines(self, nid: str,
                              relations: Optional[Set[str]] = None,
                              include_ambiguous: bool = False) -> List[dict]:
        """Return one-line caller evidence entries for inbound dependencies.

        Each entry includes caller id, relation, source file, line number and
        the exact one-line code snippet when available.
        """
        relation_set = relations or DEFAULT_IMPACT_RELATIONS
        out: List[dict] = []
        for caller, _target, data in self.g.in_edges(nid, data=True):
            relation = edge_relation(data)
            if relation not in relation_set:
                continue
            conf = str(data.get("confidence", ""))
            if not include_ambiguous and conf.lower() == "ambiguous":
                continue

            caller_node = self.g.nodes.get(caller, {})
            path = str(
                data.get("source_file")
                or caller_node.get("source_file")
                or caller_node.get("path")
                or ""
            )
            line = _line_from_source_location(data.get("source_location"))
            if not line:
                line = int(caller_node.get("start_line") or 0)

            code_line = ""
            if path and line > 0:
                full = os.path.join(self.root, path)
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as fh:
                        lines = fh.readlines()
                    if 1 <= line <= len(lines):
                        code_line = lines[line - 1].rstrip("\n")
                except OSError:
                    code_line = ""

            out.append({
                "caller_id": caller,
                "relation": relation,
                "confidence": conf,
                "file": path,
                "line": line,
                "code_line": code_line,
            })

        out.sort(key=lambda x: (x["file"], x["line"], x["caller_id"]))
        return out

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
def _class_of(node_id: str) -> str:
    """The immediate enclosing class name of a method node, or '' for a function.

    e.g. 'models.py::User.save' -> 'User'; 'a.py::Outer.Inner.m' -> 'Inner'.
    """
    qual = node_id.split("::", 1)[1] if "::" in node_id else node_id
    if "." not in qual:
        return ""
    return qual.rsplit(".", 1)[0].rsplit(".", 1)[-1]


def _resolve(g: nx.DiGraph, by_simple: Dict[str, List[str]],
             import_bindings: Dict[str, Dict[str, str]] | None = None,
             max_reexport_depth: int = 0) -> None:
    """Replace __unresolved__ placeholder targets with real node ids.

    Each resolved edge is tagged with a `confidence`, so downstream impact
    analysis can tell a sure link from a guessed one:
        "unique"    exactly one candidate, OR a same-file / imported binding
                    pins it to a single definition.
        "same_file" several candidates but one (or more) live in the caller's
                    file (or its `from x import` target) — preferred.
        "ambiguous" no same-file / imported match and several same-named
                    definitions exist; the edge is a guess fanned out to all.
    `candidates` stores how many definitions matched the simple name.
    """
    import_bindings = import_bindings or {}

    re_exports_by_file: Dict[str, Set[str]] = {}
    for src, tgt, ed in g.edges(data=True):
        if ed.get("type") == "re_exports" and "::" not in str(src) and "::" not in str(tgt):
            re_exports_by_file.setdefault(str(src), set()).add(str(tgt))

    def _expand_re_exports(start_file: str) -> Set[str]:
        if max_reexport_depth <= 0:
            return {start_file}
        visited: Set[str] = {start_file}
        queue: deque[Tuple[str, int]] = deque([(start_file, 0)])
        while queue:
            current, depth = queue.popleft()
            if depth >= max_reexport_depth:
                continue
            for nxt in re_exports_by_file.get(current, set()):
                if nxt in visited:
                    continue
                visited.add(nxt)
                queue.append((nxt, depth + 1))
        return visited

    # pre-pass: which concrete class nodes does each caller instantiate?
    # lets us resolve `u.save()` to User.save when several classes define save().
    instantiated: Dict[str, Set[str]] = {}
    for u, v, data in g.edges(data=True):
        if str(v).startswith("__unresolved__::") and data.get("type") == "instantiates":
            class_name = v.split("::", 1)[1]
            class_candidates = [
                c for c in by_simple.get(class_name, [])
                if g.nodes.get(c, {}).get("kind") in {"class", "table", "service", "route", "interface"}
            ]
            caller_file = u.split("::", 1)[0]
            same_file = [c for c in class_candidates if c.split("::", 1)[0] == caller_file]
            tgt_file = import_bindings.get(caller_file, {}).get(class_name)
            imported = [c for c in class_candidates if c.split("::", 1)[0] == tgt_file] if tgt_file else []
            chosen = same_file or imported or class_candidates
            if chosen:
                instantiated.setdefault(u, set()).update(chosen)

    to_remove: List[tuple] = []
    to_add: List[tuple] = []

    for u, v, data in list(g.edges(data=True)):
        if not str(v).startswith("__unresolved__::"):
            continue
        to_remove.append((u, v))
        name = v.split("::", 1)[1]
        module_bound = False
        module_name = ""
        member_name = name
        if "." in name:
            head, tail = name.split(".", 1)
            if tail and head in import_bindings.get(u.split("::", 1)[0], {}):
                module_bound = True
                module_name = head
                member_name = tail
        lookup_name = member_name
        candidates = by_simple.get(lookup_name, [])
        if not candidates:
            continue
        caller_file = u.split("::", 1)[0]
        # 1. prefer same-file candidates (a local def shadows an import)
        same_file = [c for c in candidates if c.split("::", 1)[0] == caller_file]
        if same_file:
            chosen = same_file
            confidence = "same_file" if len(candidates) > 1 else "unique"
        else:
            # 2. use `from mod import name` bindings to pin the right file
            binding_key = module_name if module_bound else lookup_name
            tgt_file = import_bindings.get(caller_file, {}).get(binding_key)
            imported = []
            if tgt_file:
                bound_files = _expand_re_exports(tgt_file)
                imported = [c for c in candidates if c.split("::", 1)[0] in bound_files]
            # 3. for a method call, prefer the class the caller instantiates
            by_inst = []
            if not imported and data.get("type") == "calls" and len(candidates) > 1:
                caller_classes = instantiated.get(u, set())
                by_inst = []
                for c in candidates:
                    cls = _class_of(c)
                    if not cls:
                        continue
                    cpath = c.split("::", 1)[0]
                    cls_id = f"{cpath}::{cls}"
                    if cls_id in caller_classes:
                        by_inst.append(c)
            if imported:
                chosen = imported
                confidence = "unique" if len(imported) == 1 else "same_file"
            elif by_inst:
                chosen = by_inst
                confidence = "unique" if len(by_inst) == 1 else "same_file"
            elif tgt_file:
                # Module-bound reference could not be proven in that module chain.
                # Stay conservative and do not fan out globally.
                continue
            else:
                # 4. fall back: guess (and fan out to) all same-named defs
                chosen = candidates
                confidence = "ambiguous" if len(candidates) > 1 else "unique"
        for tgt in chosen:
            if tgt != u:
                edge_data = dict(data)
                edge_data["confidence"] = confidence
                edge_data["candidates"] = len(candidates)
                to_add.append((u, tgt, edge_data))

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


def _sync_edge_contract(g: nx.DiGraph) -> None:
    """Populate canonical edge fields without breaking legacy callers."""
    for _u, _v, data in g.edges(data=True):
        relation = edge_relation(data)
        if relation:
            data.setdefault("relation", relation)
            data.setdefault("type", relation)
        legacy_conf = data.get("confidence", "unique")
        data.setdefault("confidence_kind", normalize_confidence(legacy_conf))
        data.setdefault("confidence_score", confidence_score(legacy_conf))


def _normalize_api_path(path_value: str) -> str:
    p = (path_value or "").strip()
    if not p:
        return ""
    p = p.split("?", 1)[0].strip()
    p = re.sub(r"^[a-zA-Z]+://[^/]+", "", p)
    if not p.startswith("/"):
        p = "/" + p
    p = re.sub(r"/+", "/", p)
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p.lower()


def _extract_route_signatures_from_source(src: str, lang: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    text = src or ""

    if lang == "python":
        # @app.get('/x'), @router.post('/x')
        for m in re.finditer(
            r"@\w+(?:\.\w+)*\.(get|post|put|patch|delete|head|options)\(\s*['\"]([^'\"]+)['\"]",
            text,
            flags=re.I,
        ):
            method = m.group(1).upper()
            path = _normalize_api_path(m.group(2))
            if path:
                out.append((method, path))
    elif lang == "java":
        # @GetMapping("/x") style
        for m in re.finditer(
            r"@(Get|Post|Put|Patch|Delete)Mapping\s*\(\s*['\"]([^'\"]+)['\"]",
            text,
            flags=re.I,
        ):
            method = m.group(1).upper()
            path = _normalize_api_path(m.group(2))
            if path:
                out.append((method, path))
        # @RequestMapping(value="/x", method=RequestMethod.GET)
        for m in re.finditer(
            r"@RequestMapping\s*\(([^)]*)\)",
            text,
            flags=re.I | re.S,
        ):
            payload = m.group(1)
            p = re.search(r"(?:value|path)\s*=\s*['\"]([^'\"]+)['\"]", payload, flags=re.I)
            mm = re.search(r"RequestMethod\.(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)", payload, flags=re.I)
            if p and mm:
                path = _normalize_api_path(p.group(1))
                method = mm.group(1).upper()
                if path:
                    out.append((method, path))
    elif lang in {"javascript", "typescript"}:
        # @Get('/x') style decorators
        for m in re.finditer(
            r"@(Get|Post|Put|Patch|Delete)\s*\(\s*['\"]([^'\"]+)['\"]",
            text,
            flags=re.I,
        ):
            method = m.group(1).upper()
            path = _normalize_api_path(m.group(2))
            if path:
                out.append((method, path))

    # Dedupe while preserving order
    dedup: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for sig in out:
        if sig not in seen:
            seen.add(sig)
            dedup.append(sig)
    return dedup


def _extract_client_http_calls_from_source(src: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    text = src or ""

    # requests.get('/x'), axios.post('/x'), httpx.get('/x')
    for m in re.finditer(
        r"\b(?:requests|httpx|axios)\.(get|post|put|patch|delete|head|options)\s*\(\s*['\"]([^'\"]+)['\"]",
        text,
        flags=re.I,
    ):
        method = m.group(1).upper()
        path = _normalize_api_path(m.group(2))
        if path:
            out.append((method, path))

    # fetch('/x', { method: 'POST' }) or fetch('/x') -> GET
    for m in re.finditer(
        r"\bfetch\s*\(\s*['\"]([^'\"]+)['\"](?:\s*,\s*\{([^}]*)\})?",
        text,
        flags=re.I | re.S,
    ):
        path = _normalize_api_path(m.group(1))
        opts = m.group(2) or ""
        mm = re.search(r"\bmethod\s*:\s*['\"](GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)['\"]", opts, flags=re.I)
        method = mm.group(1).upper() if mm else "GET"
        if path:
            out.append((method, path))

    # Dedupe while preserving order
    dedup: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for sig in out:
        if sig not in seen:
            seen.add(sig)
            dedup.append(sig)
    return dedup


def _attach_cross_language_api_links(cg: CodeGraph, flags: GraphFeatureFlags) -> None:
    """Optional cross-language API linker (API-first strategy).

    Disabled by default to keep existing PR/commit/file review behavior stable.
    """
    if not flags.enable_cross_language_api_links:
        return

    g = cg.g

    routes_by_signature: Dict[Tuple[str, str], List[str]] = {}
    for nid, data in g.nodes(data=True):
        if "::" not in str(nid):
            continue
        if data.get("kind") not in {"route", "function", "method", "service"}:
            continue
        lang = str(data.get("lang") or "")

        method = str(data.get("route_method") or data.get("http_method") or "").upper().strip()
        path = _normalize_api_path(str(data.get("route_path") or data.get("path_template") or ""))
        signatures: List[Tuple[str, str]] = []
        if method and path:
            signatures.append((method, path))
        else:
            try:
                src = cg.source(str(nid))
            except Exception:
                src = ""
            signatures.extend(_extract_route_signatures_from_source(src, lang))
            if not signatures:
                path_attr = str(data.get("path") or "")
                start_line = int(data.get("start_line") or 1)
                end_line = int(data.get("end_line") or start_line)
                if path_attr:
                    try:
                        ctx_src = cg.source_lines(path_attr, max(1, start_line - 4), end_line)
                    except Exception:
                        ctx_src = ""
                    if ctx_src:
                        signatures.extend(_extract_route_signatures_from_source(ctx_src, lang))

        for sig in signatures:
            routes_by_signature.setdefault(sig, []).append(str(nid))

    if not routes_by_signature:
        return

    for nid, data in g.nodes(data=True):
        if "::" not in str(nid):
            continue
        if data.get("kind") not in {"function", "method", "service"}:
            continue

        caller_lang = str(data.get("lang") or "")
        try:
            src = cg.source(str(nid))
        except Exception:
            continue

        for method, path in _extract_client_http_calls_from_source(src):
            targets = routes_by_signature.get((method, path), [])
            if len(targets) != 1:
                # Conservative: only link on unique route match.
                continue
            target = targets[0]
            target_lang = str(g.nodes.get(target, {}).get("lang") or "")
            if not target_lang or target_lang == caller_lang:
                continue
            if target == nid:
                continue
            if g.has_edge(str(nid), target):
                continue

            g.add_edge(
                str(nid),
                target,
                type="uses",
                confidence="unique",
                inferred=True,
                api_method=method,
                api_path=path,
            )


# ── build_graph ────────────────────────────────────────────────────────────────
def build_graph(
    root: str,
    max_files: int = 5000,
    backend: str = "primitive",
    feature_flags: GraphFeatureFlags | None = None,
) -> CodeGraph:
    if backend not in {"primitive", None, ""}:
        raise ValueError(
            f"Unsupported graph backend: {backend!r}. Only 'primitive' is supported."
        )
    flags = feature_flags or GraphFeatureFlags.from_env()
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
    source_index: Set[str] = {path for path, _ in source_files}
    package_index: Dict[str, List[str]] = {}
    java_package_by_file: Dict[str, str] = {}
    java_package_classes: Dict[str, Dict[str, List[str]]] = {}
    java_fqcn_index: Dict[str, str] = {}
    tsconfig_base_dir, tsconfig_paths = _load_tsconfig_aliases(root)
    for path, lang in source_files:
        if lang == "python":
            mod = path[:-3].replace("/", ".")
            module_index[mod] = path
            module_index[path] = path
            if mod.endswith(".__init__"):
                module_index[mod[: -len(".__init__")]] = path
        elif lang == "java":
            class_name = os.path.splitext(os.path.basename(path))[0]
            package_index.setdefault(class_name, []).append(path)
            full = os.path.join(root, path)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    package_name = _java_package_from_source(fh.read())
            except OSError:
                package_name = ""
            java_package_by_file[path] = package_name
            java_package_classes.setdefault(package_name, {}).setdefault(class_name, []).append(path)
            fqcn = f"{package_name}.{class_name}" if package_name else class_name
            java_fqcn_index[fqcn] = path

    # per-file imported-symbol bindings (local_name -> target file), filled by the
    # extractors and used by _resolve to disambiguate cross-file calls.
    import_bindings: Dict[str, Dict[str, str]] = {}

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
                    bindings = import_bindings.setdefault(path, {})
                    ext = _PyExtractor(path, src_bytes, g, cg._by_simple,
                                       module_index, bindings)
                    ext.extract(tree.root_node)
                else:
                    extractor_cls = _extractor_cls_for_language(lang, flags)
                    _safe_extract_non_python(
                        extractor_cls,
                        path,
                        lang,
                        src_bytes,
                        g,
                        cg._by_simple,
                        tree.root_node,
                        import_bindings,
                        package_index,
                        source_index,
                        java_package_by_file,
                        java_package_classes,
                        java_fqcn_index,
                        os.path.abspath(root).replace("\\", "/"),
                        tsconfig_base_dir,
                        tsconfig_paths,
                        flags,
                    )
            except Exception:
                # fall back to ast for Python if tree-sitter parse fails
                if lang == "python":
                    _ast_fallback(path, src_text, g, cg._by_simple, module_index,
                                  import_bindings.setdefault(path, {}))
                elif flags.strict_language_mode:
                    raise
        elif lang == "python":
            _ast_fallback(path, src_text, g, cg._by_simple, module_index,
                          import_bindings.setdefault(path, {}))

    _resolve(g, cg._by_simple, import_bindings, max_reexport_depth=flags.max_reexport_depth)
    _resolve_overrides(g)
    _attach_cross_language_api_links(cg, flags)
    _sync_edge_contract(g)
    return cg


# ── ast fallback for Python ────────────────────────────────────────────────────
def _ast_fallback(path: str, src: str, g: nx.DiGraph,
                  by_simple: Dict[str, List[str]],
                  module_index: Dict[str, str],
                  bindings: Dict[str, str] | None = None) -> None:
    """Pure-ast Python extraction (used when tree-sitter is unavailable)."""
    bindings = bindings if bindings is not None else {}
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
                    for alias in node.names:
                        local = alias.asname or alias.name
                        if local and local != "*":
                            bindings[local] = tgt

    for n in tree.body:
        visit(n)
