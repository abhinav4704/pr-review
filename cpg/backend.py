"""Deterministic extraction from a Python (FastAPI) backend using `ast`.

Produces nodes + edges for these layers:
  CONTAINS      file -> class -> function
  CALLS         function -> function  (unresolved; resolved in resolve.py)
  HANDLES       route -> handler function
  GUARDED_BY    route -> auth dependency  (placeholder; resolved in resolve.py)
  DEPENDS_ON    function -> dependency  (all Depends(); placeholder)
  READS_TABLE / WRITES_TABLE  function -> table
  ACQUIRES      function -> lock
  IMPORTS       file -> external module
  DECORATES     decorator -> function/class/route
  VALIDATES_WITH route -> Pydantic model class  (heuristic; see KNOWN_FAILURE below)
  INHERITS      class -> base class  (placeholder)
  RAISES        function -> exception class  (placeholder)

KNOWN_FAILURE — VALIDATES_WITH: only ast.Name annotations are captured.
Optional[X], List[X], Union[X,Y] generics are unwrapped one level but nested
parameterised types are skipped.
"""
from __future__ import annotations
import ast
import os
import re
from . import model as M


# Patterns we treat as HTTP method names on a router/app decorator.
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}

# ORM / driver call names mapped to read vs write intent.
# Only genuine query-initiating verbs — NOT .get()/.all()/.first()/.scalar() which
# chain off .query() and are overwhelmingly non-DB (dict access, etc.).
DB_READ_CALLS = {"query", "select", "scalars", "aggregate",
                 "find", "find_one", "count_documents"}
DB_WRITE_CALLS = {"add", "add_all", "merge", "delete", "insert_one", "insert_many",
                  "update_one", "update_many", "replace_one", "delete_one",
                  "delete_many", "bulk_write", "insert", "update", "execute_write"}

LOCK_FACTORIES = {"Lock", "RLock", "Semaphore", "BoundedSemaphore", "Condition"}
LOCK_ACQUIRE_METHODS = {"acquire"}

# Auth-guard detection — only Depends() arguments whose names suggest
# authentication/authorisation are emitted as GUARDED_BY edges.  Session
# providers (get_db, get_session) and pagination helpers are excluded.
_AUTH_KEYWORDS = {"user", "auth", "login", "identity", "role", "permission",
                  "current", "require", "token", "jwt", "bearer", "rbac",
                  "admin", "scope"}


def _is_auth_dep(name: str) -> bool:
    return bool(name) and any(k in name.lower() for k in _AUTH_KEYWORDS)

_SQL_TABLE = re.compile(
    r"\b(?:from|into|update|join)\s+([a-zA-Z_][\w]*)", re.IGNORECASE)
_SQL_VERB = re.compile(r"\b(select|insert|update|delete)\b", re.IGNORECASE)


def _name_of(node) -> str | None:
    """Dotted name for a Name/Attribute expression, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _str_const(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def module_dotted(relpath: str, backend_root: str) -> str:
    """backend/app/services/x.py (root 'backend') -> app.services.x"""
    rel = relpath.replace("\\", "/")
    if backend_root and rel.startswith(backend_root.rstrip("/") + "/"):
        rel = rel[len(backend_root.rstrip("/")) + 1:]
    rel = rel[:-3] if rel.endswith(".py") else rel
    rel = rel[:-len("/__init__")] if rel.endswith("/__init__") else rel
    return rel.replace("/", ".")


class PyExtractor(ast.NodeVisitor):
    def __init__(self, relpath: str, backend_root: str):
        self.relpath = relpath
        self.module = module_dotted(relpath, backend_root)
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        # imports: local alias -> dotted target (module or module.symbol)
        self.imports: dict[str, str] = {}
        # module-level lock variables: name -> lock node id
        self.module_locks: dict[str, str] = {}
        # exported callables of this module: bare name -> func node id
        self.exports: dict[str, str] = {}
        # context stack of (kind, qualname-prefix, func_node_id)
        self._class_stack: list[str] = []
        self._func_stack: list[str] = []  # func node ids

        # include_router(...) calls detected at module level
        self.router_includes: list[dict] = []

        self.file_node_id = M.file_id(relpath)
        self.nodes.append(M.node(self.file_node_id, "File", os.path.basename(relpath),
                                 relpath, 1, module=self.module))

    # -- module-level expressions (include_router detection) --------------
    def visit_Expr(self, node: ast.Expr):
        if not isinstance(node.value, ast.Call):
            return
        call = node.value
        if not (isinstance(call.func, ast.Attribute)
                and call.func.attr == "include_router"):
            return
        if not call.args:
            return
        target_name = _name_of(call.args[0])
        if not target_name:
            return
        # target_name may be "developer_assistant.router" — take the leading var
        target_var = target_name.split(".")[0]
        prefix = ""
        for kw in call.keywords:
            if kw.arg == "prefix":
                prefix = _str_const(kw.value) or ""
        self.router_includes.append({"target_var": target_var, "prefix": prefix})

    # -- imports -----------------------------------------------------------
    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            self.imports[local] = alias.name
            self.edges.append(M.edge(self.file_node_id, M.external_id(alias.name),
                                     "IMPORTS", line=node.lineno))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        base = node.module or ""
        for alias in node.names:
            local = alias.asname or alias.name
            self.imports[local] = f"{base}.{alias.name}" if base else alias.name
        if base:
            self.edges.append(M.edge(self.file_node_id, M.external_id(base),
                                     "IMPORTS", line=node.lineno))
        self.generic_visit(node)

    # -- classes -----------------------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef):
        cid = M.class_id(self.relpath, node.name)
        bases = [b for b in (_name_of(x) for x in node.bases) if b]
        line_end = _end_line(node)
        self.nodes.append(M.node(cid, "Class", node.name, self.relpath, node.lineno,
                                 line_end=line_end, bases=bases))
        self.edges.append(M.edge(self.file_node_id, cid, "CONTAINS"))
        # INHERITS placeholders — resolved in resolve.py to Class or ExternalSymbol
        for base in bases:
            self.edges.append(M.edge(f"__class__::{cid}", f"__baseref__::{base}",
                                     "INHERITS", _class_id=cid, _base_name=base))
        # DECORATES edges for class decorators
        self._scan_decorators(node, cid)
        self._class_stack.append(node.name)
        # detect module-level lock assignments inside class body too
        for stmt in node.body:
            self._maybe_lock_assign(stmt, owner=node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    # -- functions ---------------------------------------------------------
    def visit_FunctionDef(self, node):
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._handle_function(node)

    def _handle_function(self, node):
        prefix = ".".join(self._class_stack)
        qual = f"{prefix}.{node.name}" if prefix else node.name
        fid = M.func_id(self.relpath, qual)
        is_method = bool(self._class_stack)
        line_end = _end_line(node)
        self.nodes.append(M.node(fid, "Function", qual, self.relpath, node.lineno,
                                 line_end=line_end,
                                 is_method=is_method,
                                 is_async=isinstance(node, ast.AsyncFunctionDef)))
        parent = (M.class_id(self.relpath, self._class_stack[-1])
                  if is_method else self.file_node_id)
        self.edges.append(M.edge(parent, fid, "CONTAINS"))
        if not is_method:
            self.exports[node.name] = fid

        # ---- route + authz from decorators / signature ----
        self._scan_route(node, fid)
        self._scan_auth(node, fid)
        # ---- DECORATES edges for function decorators ----
        self._scan_decorators(node, fid)
        # ---- VALIDATES_WITH from parameter/return annotations ----
        self._scan_validates(node, fid)
        # ---- RAISES from raise statements in body ----
        self._scan_raises(node, fid)

        # ---- walk body for calls / db / locks ----
        self._func_stack.append(fid)
        for child in node.body:
            for sub in ast.walk(child):
                if isinstance(sub, ast.Call):
                    self._scan_call(sub, fid)
                elif isinstance(sub, ast.With) or isinstance(sub, ast.AsyncWith):
                    self._scan_with(sub, fid)
            self._maybe_lock_assign(child, owner=None)
        self._func_stack.pop()
        # don't generic_visit into nested funcs twice: handle nesting explicitly
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(child)

    # -- route detection ---------------------------------------------------
    def _scan_route(self, node, fid):
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            dname = _name_of(dec.func)  # e.g. router.get / app.post
            if not dname or "." not in dname:
                continue
            method = dname.rsplit(".", 1)[1].lower()
            if method not in HTTP_METHODS:
                continue
            path = None
            if dec.args:
                path = _str_const(dec.args[0])
            for kw in dec.keywords:
                if kw.arg == "path":
                    path = _str_const(kw.value) or path
            if path is None:
                continue
            rid = M.route_id(method, path, self.relpath)
            line_end = _end_line(node)
            self.nodes.append(M.node(rid, "Route", f"{method.upper()} {path}",
                                     self.relpath, node.lineno,
                                     line_end=line_end,
                                     method=method.upper(),
                                     path=M.normalize_path(path)))
            self.edges.append(M.edge(rid, fid, "HANDLES"))

    # -- authz detection (FastAPI Depends + role helpers) ------------------
    def _scan_auth(self, node, fid):
        # default values of args may be Depends(...)
        defaults = list(node.args.defaults) + list(node.args.kw_defaults)
        guards = []
        for d in defaults:
            if isinstance(d, ast.Call) and _name_of(d.func) in ("Depends", "Security"):
                if d.args:
                    inner = d.args[0]
                    # Depends(get_current_user) -> Name
                    # Depends(require_role("admin")) -> Call, dep is the callee
                    if isinstance(inner, ast.Call):
                        dep = _name_of(inner.func)
                        role = _role_arg(inner)
                    else:
                        dep = _name_of(inner)
                        role = None
                    if dep:
                        guards.append((dep, role))
        # decorator-style guards: @requires_role("admin"), @login_required
        for dec in node.decorator_list:
            dn = _name_of(dec.func) if isinstance(dec, ast.Call) else _name_of(dec)
            if dn and any(k in dn.lower() for k in ("require", "login_required",
                                                    "role", "auth", "permission")):
                role = None
                if isinstance(dec, ast.Call) and dec.args:
                    role = _str_const(dec.args[0])
                guards.append((dn, role))
        if not guards:
            return
        # GUARDED_BY: only real auth guards (filtered by keyword).
        # DEPENDS_ON: all Depends() targets regardless of auth filter.
        for dep, role in guards:
            # DEPENDS_ON — always emit for every Depends() target
            self.edges.append(M.edge(f"__handler__::{fid}",
                                     f"__depref__::{dep}",
                                     "DEPENDS_ON", _dep_name=dep))
            # GUARDED_BY — only when dep name looks like an auth guard
            if not _is_auth_dep(dep):
                continue
            self.edges.append(M.edge(f"__handler__::{fid}",
                                     f"__authref__::{dep}",
                                     "GUARDED_BY", role=role or "", _dep_name=dep))

    # -- call detection (unresolved) --------------------------------------
    def _scan_call(self, call: ast.Call, fid):
        name = _name_of(call.func)
        if not name:
            return
        # database?
        if self._scan_db(call, name, fid):
            return
        # lock.acquire()?
        if isinstance(call.func, ast.Attribute) and call.func.attr in LOCK_ACQUIRE_METHODS:
            base = _name_of(call.func.value)
            if base:
                self._link_lock(base, fid)
        # generic call -> emit unresolved CALLS
        if isinstance(call.func, ast.Name):
            ref = {"kind": "name", "name": name}
        elif isinstance(call.func, ast.Attribute):
            base = _name_of(call.func.value)
            ref = {"kind": "attr", "base": base or "", "attr": call.func.attr}
        else:
            return
        self.edges.append(M.edge(fid, "__unresolved__", "CALLS",
                                 raw=ref, line=getattr(call, "lineno", 0)))

    # -- db detection ------------------------------------------------------
    def _scan_db(self, call: ast.Call, name: str, fid) -> bool:
        # Belt-and-suspenders: dict/object .get("key") is NOT a table read.
        # This guards against payload.get("team_id") creating fake Table nodes.
        if (isinstance(call.func, ast.Attribute) and call.func.attr == "get"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)):
            return False
        if not isinstance(call.func, ast.Attribute):
            # raw sql via execute(...) sometimes a bare name; check string args
            return self._scan_raw_sql(call, fid)
        method = call.func.attr
        intent = None
        if method in DB_READ_CALLS:
            intent = "READS_TABLE"
        elif method in DB_WRITE_CALLS:
            intent = "WRITES_TABLE"
        if intent is None:
            return self._scan_raw_sql(call, fid)
        # mongo style: db.<collection>.insert_one(...)  -> collection name is base attr
        table = None
        if method in {"insert_one", "insert_many", "update_one", "update_many",
                      "delete_one", "delete_many", "find", "find_one", "aggregate",
                      "count_documents", "replace_one", "bulk_write"}:
            base = call.func.value
            if isinstance(base, ast.Attribute):
                table = base.attr
            elif isinstance(base, ast.Subscript):  # db["records"]
                table = _str_const(base.slice)
        # For ORM instance operations, the argument is typically an object
        # reference (variable), not a table name. Guard against fake Table nodes:
        #   db.session.delete(user_obj)
        #   db.session.add(record)
        #   db.session.add_all(records)
        #   db.session.merge(entity)
        # These should not emit table::user_obj/table::record etc.
        if (method in {"add", "add_all", "delete", "merge"}
                and call.args
                and isinstance(call.args[0], ast.Name)):
            return False
        # sqlalchemy style: db.query(Model) / select(Model)
        if table is None and call.args:
            table = _name_of(call.args[0]) or _str_const(call.args[0])
            if table and "." in table:
                table = table.rsplit(".", 1)[1]
        if not table:
            return self._scan_raw_sql(call, fid)
        tid = M.table_id(table)
        self.nodes.append(M.node(tid, "Table", table))
        self.edges.append(M.edge(fid, tid, intent, via=method,
                                 line=getattr(call, "lineno", 0)))
        return True

    def _scan_raw_sql(self, call: ast.Call, fid) -> bool:
        hit = False
        for a in call.args:
            s = _str_const(a)
            if not s or not _SQL_VERB.search(s):
                continue
            verb = _SQL_VERB.search(s).group(1).lower()
            intent = "READS_TABLE" if verb == "select" else "WRITES_TABLE"
            for tbl in _SQL_TABLE.findall(s):
                tid = M.table_id(tbl)
                self.nodes.append(M.node(tid, "Table", tbl))
                self.edges.append(M.edge(fid, tid, intent, via="rawsql",
                                         line=getattr(call, "lineno", 0)))
                hit = True
        return hit

    # -- locks -------------------------------------------------------------
    def _maybe_lock_assign(self, stmt, owner):
        if not isinstance(stmt, ast.Assign):
            return
        val = stmt.value
        if isinstance(val, ast.Call):
            fac = _name_of(val.func)
            short = fac.rsplit(".", 1)[1] if fac and "." in fac else fac
            if short in LOCK_FACTORIES:
                for tgt in stmt.targets:
                    tname = _name_of(tgt)
                    if not tname:
                        continue
                    key = tname.split(".")[-1]
                    lid = M.lock_id(self.relpath, key)
                    self.nodes.append(M.node(lid, "Lock", key, self.relpath,
                                             stmt.lineno, kind_detail=short))
                    self.module_locks[key] = lid

    # -- decorators (DECORATES edges) ------------------------------------
    def _scan_decorators(self, node, target_id: str):
        for dec in node.decorator_list:
            dec_name = None
            dec_arg = ""
            if isinstance(dec, ast.Call):
                dec_name = _name_of(dec.func)
                if dec.args:
                    dec_arg = _str_const(dec.args[0]) or ""
            else:
                dec_name = _name_of(dec)
            if not dec_name:
                continue
            did = M.decorator_id(self.relpath, dec_name, target_id)
            self.nodes.append(M.node(did, "Decorator", dec_name,
                                     self.relpath, node.lineno,
                                     dec_name=dec_name, arg=dec_arg))
            self.edges.append(M.edge(did, target_id, "DECORATES"))

    # -- VALIDATES_WITH (Pydantic request/response models) ----------------
    def _scan_validates(self, node, fid: str):
        # KNOWN_FAILURE: only statically-resolvable ast.Name annotations captured.
        # Optional[X]/List[X] unwrapped one level; deeper generics skipped.
        handler_id = f"__handler__::{fid}"
        for arg in list(node.args.args) + list(node.args.kwonlyargs):
            ann = arg.annotation
            if ann is None:
                continue
            name = _unwrap_annotation(ann)
            if name and name.endswith(_PYDANTIC_SUFFIXES):
                self.edges.append(M.edge(handler_id,
                                         f"__pydantic__::{name}",
                                         "VALIDATES_WITH", _model_name=name))
        if node.returns:
            name = _unwrap_annotation(node.returns)
            if name and name.endswith(_PYDANTIC_SUFFIXES):
                self.edges.append(M.edge(handler_id,
                                         f"__pydantic__::{name}",
                                         "VALIDATES_WITH", _model_name=name,
                                         direction="response"))

    # -- RAISES (raised exception types) ----------------------------------
    def _scan_raises(self, node, fid: str):
        for child in ast.walk(node):
            if not isinstance(child, ast.Raise) or child.exc is None:
                continue
            exc = child.exc
            if isinstance(exc, ast.Call):
                exc_name = _name_of(exc.func)
            else:
                exc_name = _name_of(exc)
            if exc_name:
                self.edges.append(M.edge(fid, f"__excref__::{exc_name}",
                                         "RAISES", _exc_name=exc_name,
                                         line=getattr(child, "lineno", 0)))

    def _scan_with(self, w, fid):
        items = w.items
        for it in items:
            base = _name_of(it.context_expr)
            if base:
                self._link_lock(base, fid)

    def _link_lock(self, base: str, fid):
        key = base.split(".")[-1]
        lid = self.module_locks.get(key)
        if lid is None:
            # forward/self lock we haven't seen declared; create a soft node
            lid = M.lock_id(self.relpath, key)
            self.nodes.append(M.node(lid, "Lock", key, self.relpath, 0,
                                     kind_detail="inferred"))
            self.module_locks[key] = lid
        self.edges.append(M.edge(fid, lid, "ACQUIRES"))


def _role_arg(callnode) -> str | None:
    # require_role("admin") used inside Depends(require_role("admin"))
    if isinstance(callnode, ast.Call) and callnode.args:
        return _str_const(callnode.args[0])
    return None


def _end_line(node: ast.AST) -> int:
    """Return end_lineno if available; fall back to max child lineno."""
    if hasattr(node, "end_lineno") and node.end_lineno:
        return node.end_lineno
    return max(
        (getattr(child, "lineno", node.lineno) for child in ast.walk(node)),
        default=node.lineno,
    )


def _unwrap_annotation(ann) -> str | None:
    """Extract a plain class name from an annotation AST node.

    Handles:
      ast.Name      -> name
      ast.Attribute -> attr  (e.g. models.Foo -> Foo)
      ast.Subscript -> unwrap one level: Optional[X] -> recurse on X
    Returns None for complex generics we cannot resolve.
    """
    if isinstance(ann, ast.Name):
        return ann.id
    if isinstance(ann, ast.Attribute):
        return ann.attr
    if isinstance(ann, ast.Subscript):
        # e.g. Optional[Model] or List[Model]
        return _unwrap_annotation(ann.slice)
    return None


# Heuristic: class names ending with these suffixes are treated as Pydantic models
# for VALIDATES_WITH edge emission.
# KNOWN_FAILURE: "Model" and "Schema" may also match SQLAlchemy/Marshmallow classes.
# "In" and "Out" removed — too broad: LogIn, SignIn, PrintOut etc. are non-Pydantic.
_PYDANTIC_SUFFIXES = ("Request", "Response", "Schema", "Body", "Model")


def _collect_router_includes(tree: ast.AST) -> list[dict]:
    """Collect include_router(target, prefix=...) calls from the whole file.

    This catches includes declared at module level and inside app factory
    functions (which are not visited via generic NodeVisitor flow here).
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute)
                and node.func.attr == "include_router"):
            continue
        if not node.args:
            continue
        target_name = _name_of(node.args[0])
        if not target_name:
            continue
        target_var = target_name.split(".")[0]
        prefix = ""
        for kw in node.keywords:
            if kw.arg == "prefix":
                prefix = _str_const(kw.value) or ""
        key = (target_var, prefix)
        if key in seen:
            continue
        seen.add(key)
        out.append({"target_var": target_var, "prefix": prefix})
    return out


def extract_python(relpath: str, source: str, backend_root: str) -> dict:
    tree = ast.parse(source)
    ex = PyExtractor(relpath, backend_root)
    # visit top-level only; _handle_function recurses for nested defs
    for n in tree.body:
        ex.visit(n)
    # Ensure include_router calls are captured even when declared inside
    # app factory functions.
    ex.router_includes = _collect_router_includes(tree)
    return {
        "nodes": ex.nodes,
        "edges": ex.edges,
        "imports": ex.imports,
        "exports": ex.exports,
        "module": ex.module,
        "relpath": relpath,
        "router_includes": ex.router_includes,
    }