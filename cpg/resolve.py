"""Whole-repo resolution passes.

1. Resolve unresolved CALLS edges to internal function nodes (or external stubs).
2. Re-key GUARDED_BY: placeholder handler/auth refs -> real route + auth func nodes.
3. Re-key DEPENDS_ON: handler ref -> real func/external for every Depends() dep.
4. Re-key VALIDATES_WITH: handler ref -> Class or ExternalSymbol (Pydantic model).
5. Re-key INHERITS: class placeholder -> Class or ExternalSymbol.
6. Re-key RAISES: func -> Class or ExternalSymbol.
7. Re-key RENDERS/USES_COMPONENT: component -> Component or ExternalSymbol.
8. Prefix pass: apply include_router(prefix=...) chains to route paths and IDs.
9. Bridge: match FrontendCall nodes to Route nodes on method + normalised path.
"""
from __future__ import annotations
from . import model as M


def build(per_file: list[dict], fe_files: list[dict]) -> tuple[list[dict], list[dict]]:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    # ---- index symbols across the repo --------------------------------
    # module dotted -> {bare func name -> node id}
    module_exports: dict[str, dict[str, str]] = {}
    # relpath -> {bare func name -> node id}   (top-level)
    file_funcs: dict[str, dict[str, str]] = {}
    # relpath -> {ClassName -> {method -> node id}}
    class_methods: dict[str, dict[str, dict[str, str]]] = {}
    # function node id -> relpath (for "same module" resolution)
    func_file: dict[str, str] = {}
    # relpath -> import table
    file_imports: dict[str, dict[str, str]] = {}
    # relpath -> module dotted
    file_module: dict[str, str] = {}
    # class bare name -> class node id (last-write wins; good enough for resolution)
    class_index: dict[str, str] = {}
    # component bare name -> component node id
    comp_index: dict[str, str] = {}

    def add_node(n):
        nodes.setdefault(n["id"], n)

    for pf in per_file:
        relpath = pf["relpath"]
        module_exports[pf["module"]] = pf["exports"]
        file_funcs[relpath] = pf["exports"]
        file_imports[relpath] = pf["imports"]
        file_module[relpath] = pf["module"]
        for n in pf["nodes"]:
            add_node(n)
            if n["kind"] == "Function":
                func_file[n["id"]] = relpath
                qual = n["name"]
                if "." in qual:  # method
                    cls, meth = qual.rsplit(".", 1)
                    class_methods.setdefault(relpath, {}).setdefault(cls, {})[meth] = n["id"]
            elif n["kind"] == "Class":
                # class_index: bare name -> id (last writer wins for duplicates)
                class_index[n["name"]] = n["id"]
            elif n["kind"] == "Component":
                comp_index[n["name"]] = n["id"]

    for fe in fe_files:
        for n in fe["nodes"]:
            add_node(n)
            if n["kind"] == "Component":
                comp_index[n["name"]] = n["id"]

    # ---- PREFIX PASS: apply include_router chains ----------------------
    # Compute accumulated prefix per module (from app root downward).
    accumulated = _compute_module_prefixes(per_file)

    # Build raw route -> handler func mapping (before any remapping).
    raw_route_to_func: dict[str, str] = {}
    for pf in per_file:
        for e in pf["edges"]:
            if e["type"] == "HANDLES":
                raw_route_to_func[e["src"]] = e["dst"]

    # For each Route node, prepend the module's accumulated prefix.
    route_remap: dict[str, str] = {}  # old_id -> new_id
    for nid in list(nodes.keys()):
        n = nodes[nid]
        if n["kind"] != "Route":
            continue
        handler_fid = raw_route_to_func.get(nid)
        relpath = func_file.get(handler_fid or "", "") or n.get("file", "")
        module = file_module.get(relpath, "")
        prefix = accumulated.get(module, "")
        if not prefix:
            continue
        old_path = n["path"]          # already-normalized local path
        full_path = M.normalize_path(prefix + old_path)
        new_id = M.route_id(n["method"], full_path, n.get("file", ""))
        if new_id == nid:
            continue
        n["id"] = new_id
        n["path"] = full_path
        n["name"] = f"{n['method']} {full_path}"
        nodes[new_id] = n
        del nodes[nid]
        route_remap[nid] = new_id

    # ---- pass 1: resolve CALLS ----------------------------------------
    # Rebuild handler_of_fid using remapped route ids.
    handler_of_fid: dict[str, str] = {}   # func_id -> route_id
    for pf in per_file:
        for e in pf["edges"]:
            if e["type"] == "HANDLES":
                new_rid = route_remap.get(e["src"], e["src"])
                handler_of_fid[e["dst"]] = new_rid

    for pf in per_file:
        relpath = pf["relpath"]
        imports = pf["imports"]
        for e in pf["edges"]:
            t = e["type"]
            if t == "CALLS":
                dst = _resolve_call(e, relpath, imports, file_funcs,
                                    class_methods, module_exports, add_node)
                if dst:
                    edges.append(M.edge(e["src"], dst, "CALLS",
                                        line=e.get("line", 0)))
                # unresolved calls are dropped (stdlib/builtins/noise)
            elif t == "GUARDED_BY":
                # src looks like __handler__::<fid>, dst like __authref__::<dep>
                fid = e["src"].split("::", 1)[1]
                dep = e.get("_dep_name", "")
                rid = handler_of_fid.get(fid)
                if not rid:
                    continue
                auth_id = _resolve_symbol(dep, relpath, imports, file_funcs,
                                          module_exports)
                if not auth_id:
                    auth_id = M.external_id(dep)
                    add_node(M.node(auth_id, "ExternalSymbol", dep))
                edges.append(M.edge(rid, auth_id, "GUARDED_BY",
                                    role=e.get("role", "")))
            elif t == "DEPENDS_ON":
                # src: __handler__::<fid>, dep: all Depends() targets (no auth filter)
                fid = e["src"].split("::", 1)[1]
                dep = e.get("_dep_name", "")
                rid = handler_of_fid.get(fid)
                if not rid:
                    continue
                dep_id = _resolve_symbol(dep, relpath, imports, file_funcs,
                                         module_exports)
                if not dep_id:
                    dep_id = M.external_id(dep)
                    add_node(M.node(dep_id, "ExternalSymbol", dep))
                edges.append(M.edge(rid, dep_id, "DEPENDS_ON"))
            elif t == "VALIDATES_WITH":
                # src: __handler__::<fid>, dst: __pydantic__::<name>
                fid = e["src"].split("::", 1)[1]
                model_name = e.get("_model_name", "")
                rid = handler_of_fid.get(fid)
                if not rid:
                    continue
                model_id = class_index.get(model_name)
                if not model_id:
                    model_id = M.external_id(model_name)
                    add_node(M.node(model_id, "ExternalSymbol", model_name))
                kw = {k: v for k, v in e.items()
                      if k not in ("src", "dst", "type", "_model_name")}
                edges.append(M.edge(rid, model_id, "VALIDATES_WITH", **kw))
            elif t == "INHERITS":
                # src: __class__::<cid>, dst: __baseref__::<name>
                cid = e.get("_class_id", "")
                base_name = e.get("_base_name", "")
                if not cid:
                    continue
                base_id = class_index.get(base_name)
                if not base_id:
                    base_id = M.external_id(base_name)
                    add_node(M.node(base_id, "ExternalSymbol", base_name))
                edges.append(M.edge(cid, base_id, "INHERITS"))
            elif t == "RAISES":
                # src: func node id, dst: __excref__::<name>
                exc_name = e.get("_exc_name", "")
                if not exc_name:
                    continue
                exc_id = class_index.get(exc_name)
                if not exc_id:
                    exc_id = _resolve_symbol(exc_name, relpath, imports,
                                              file_funcs, module_exports)
                if not exc_id:
                    exc_id = M.external_id(exc_name)
                    add_node(M.node(exc_id, "ExternalSymbol", exc_name))
                edges.append(M.edge(e["src"], exc_id, "RAISES",
                                    line=e.get("line", 0)))
            elif t == "DECORATES":
                # emitted directly with real node ids; just pass through
                edges.append(e)
            elif t in ("CONTAINS", "HANDLES", "READS_TABLE", "WRITES_TABLE",
                       "ACQUIRES", "IMPORTS"):
                if t == "IMPORTS":
                    add_node(M.node(e["dst"], "ExternalModule",
                                    e["dst"].split("::", 1)[1]))
                # Apply route_remap to src/dst in case a HANDLES edge src changed.
                e2 = dict(e)
                e2["src"] = route_remap.get(e2["src"], e2["src"])
                e2["dst"] = route_remap.get(e2["dst"], e2["dst"])
                edges.append(e2)

    # ---- pass 2: frontend -> backend bridge ---------------------------
    # Build route indexes for frontend -> backend bridging.
    # 1) route_index_exact: exact (METHOD, full_path) -> route_id
    # 2) route_alias_index: suffix alias (METHOD, suffix_path) -> set(route_id)
    # Routes have been fully prefixed by the pass above.
    route_index_exact: dict[tuple[str, str], str] = {}
    route_alias_index: dict[tuple[str, str], set[str]] = {}
    for nid, n in nodes.items():
        if n["kind"] == "Route":
            route_index_exact[(n["method"], n["path"])] = nid
            # Frontend base-URL variables (${BASE_URL}, ${CR}, ${this.baseUrl}) may
            # absorb not just the host but also a service/version prefix.
            # e.g. CR = `${BASE_URL}/v1/code-review`
            #   -> fetch(`${CR}/repositories`) strips CR -> FrontendCall path = /repositories
            #   -> route full path = /dev_assistant/api/v1/code-review/repositories
            # We register a suffix alias at EVERY segment boundary:
            #   /api/v1/code-review/repositories
            #   /v1/code-review/repositories
            #   /code-review/repositories
            #   /repositories
            # Multi-service safety: aliases are collected to sets and only used when
            # there is a single unique candidate for a frontend key.
            parts = [p for p in n["path"].split("/") if p]
            for i in range(len(parts)):
                alias = "/" + "/".join(parts[i:])
                key = (n["method"], alias)
                route_alias_index.setdefault(key, set()).add(nid)

    for fe in fe_files:
        for e in fe["edges"]:
            t = e["type"]
            if t == "RENDERS" or t == "USES_COMPONENT":
                parent_id = e.get("_parent", "")
                child_name = e.get("_child_name", "")
                if not parent_id or not child_name:
                    continue
                child_id = comp_index.get(child_name)
                if not child_id:
                    child_id = M.external_id(child_name)
                    add_node(M.node(child_id, "ExternalSymbol", child_name))
                edges.append(M.edge(parent_id, child_id, t))
            else:
                edges.append(e)
        for n in fe["nodes"]:
            if n["kind"] != "FrontendCall":
                continue
            key = (n["method"], n["path"])
            rid = route_index_exact.get(key)
            if not rid:
                candidates = route_alias_index.get(key, set())
                if len(candidates) == 1:
                    rid = next(iter(candidates))
                elif len(candidates) > 1:
                    # Ambiguous suffix in multi-service graph: do not auto-link.
                    n["bridge_resolved"] = False
                    n["bridge_ambiguous"] = True
                    n["bridge_candidates"] = len(candidates)
            if rid:
                edges.append(M.edge(n["id"], rid, "CALLS_ENDPOINT",
                                    resolved=True))
            else:
                n["bridge_resolved"] = False

    # dedupe edges
    edges = _dedupe(edges)
    return list(nodes.values()), edges


# ---------------------------------------------------------------------------
# Prefix computation
# ---------------------------------------------------------------------------

def _compute_module_prefixes(per_file: list[dict]) -> dict[str, str]:
    """Return module_dotted -> accumulated prefix from the app root.

    Walks include_router(..., prefix=...) records collected per file and
    propagates prefixes transitively from the root module downward.
    E.g.  routes.py includes developer_assistant with /v1/chat,
          main.py includes routes with /api
    => developer_assistant routes get /api/v1/chat.
    """
    all_modules: set[str] = {pf["module"] for pf in per_file}
    file_imports_map: dict[str, dict[str, str]] = {
        pf["relpath"]: pf["imports"] for pf in per_file
    }

    # include_edges: (child_module, direct_prefix, including_module)
    include_edges: list[tuple[str, str, str]] = []
    for pf in per_file:
        relpath = pf["relpath"]
        imports = file_imports_map[relpath]
        including_module = pf["module"]
        for inc in pf.get("router_includes", []):
            target_var = inc["target_var"]
            prefix = inc["prefix"]
            if not target_var or target_var not in imports:
                continue
            dotted = imports[target_var]   # e.g. "app.developer_assistant"
            # Match to a known extracted module key (handles app-prefixed imports
            # when extracted modules are backend-root-relative).
            child_module = _match_known_module(dotted, all_modules)
            if child_module is None:
                parent = dotted.rpartition(".")[0]
                child_module = _match_known_module(parent, all_modules)
            if child_module:
                include_edges.append((child_module, prefix, including_module))

    # Build child -> list of (direct_prefix, parent_module)
    child_to_parents: dict[str, list[tuple[str, str]]] = {}
    for child, prefix, parent in include_edges:
        child_to_parents.setdefault(child, []).append((prefix, parent))

    # Propagate accumulated prefixes (iterates until stable; depth-bounded).
    accumulated: dict[str, str] = {m: "" for m in all_modules}
    changed = True
    iters = 0
    while changed and iters < 50:
        changed = False
        iters += 1
        for child, parents in child_to_parents.items():
            for direct_prefix, parent in parents:
                parent_acc = accumulated.get(parent, "")
                candidate = parent_acc + direct_prefix
                current = accumulated.get(child, "")
                # Keep the deepest known chain; update if we discovered
                # a longer composed prefix (e.g. /api/v1 over /v1).
                if candidate and len(candidate) > len(current):
                    accumulated[child] = candidate
                    changed = True
    return accumulated


# ---------------------------------------------------------------------------
# Call resolution helpers
# ---------------------------------------------------------------------------

def _resolve_call(e, relpath, imports, file_funcs, class_methods,
                  module_exports, add_node):
    raw = e.get("raw", {})
    kind = raw.get("kind")
    if kind == "name":
        return _resolve_symbol(raw["name"], relpath, imports, file_funcs,
                               module_exports)
    if kind == "attr":
        base = raw.get("base", "")
        attr = raw.get("attr", "")
        # self.method() -> look up method in any class of this file
        if base in ("self", "cls"):
            for cls, methods in class_methods.get(relpath, {}).items():
                if attr in methods:
                    return methods[attr]
            return None
        # module.func() where module is imported
        if base in imports:
            target_module = _match_module_export_key(module_exports, imports[base])
            exp = module_exports.get(target_module)
            if exp and attr in exp:
                return exp[attr]
        return None
    return None


def _resolve_symbol(name, relpath, imports, file_funcs, module_exports):
    """Resolve a bare callable name to an internal function node id."""
    # same-file top-level function?
    local = file_funcs.get(relpath, {})
    if name in local:
        return local[name]
    # imported symbol? from x.y import name  -> imports[name] == "x.y.name"
    if name in imports:
        dotted = imports[name]
        mod, _, sym = dotted.rpartition(".")
        mod = _match_module_export_key(module_exports, mod)
        exp = module_exports.get(mod)
        if exp and sym in exp:
            return exp[sym]
    return None


def _match_known_module(dotted: str, all_modules: set[str]) -> str | None:
    if dotted in all_modules:
        return dotted
    for m in all_modules:
        if dotted.endswith("." + m):
            return m
    return None


def _match_module_export_key(module_exports: dict[str, dict[str, str]], dotted: str) -> str:
    if dotted in module_exports:
        return dotted
    for m in module_exports:
        if dotted.endswith("." + m):
            return m
    return dotted


def _dedupe(edges):
    seen = set()
    out = []
    for e in edges:
        k = (e["src"], e["dst"], e["type"], e.get("via", ""), e.get("role", ""))
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out
