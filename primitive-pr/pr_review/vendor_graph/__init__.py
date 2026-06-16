"""graphify - extract · build · cluster · analyze · report."""


def __getattr__(name):
    # Lazy imports so `graphify install` works before heavy deps are in place.
    _map = {
        "extract": (".extract", "extract"),
        "collect_files": (".extract", "collect_files"),
        "build_from_json": (".build", "build_from_json"),
        "cluster": (".cluster", "cluster"),
        "score_all": (".cluster", "score_all"),
        "cohesion_score": (".cluster", "cohesion_score"),
        "god_nodes": (".analyze", "god_nodes"),
        "surprising_connections": (".analyze", "surprising_connections"),
        "suggest_questions": (".analyze", "suggest_questions"),
        "generate": (".report", "generate"),
        "to_json": (".export", "to_json"),
        "to_html": (".export", "to_html"),
        "to_svg": (".export", "to_svg"),
        "to_canvas": (".export", "to_canvas"),
        "to_wiki": (".wiki", "to_wiki"),
    }
    if name in _map:
        import importlib
        mod_name, attr = _map[name]
        mod = importlib.import_module(mod_name, package=__name__)
        return getattr(mod, attr)
    raise AttributeError(f"module 'graphify' has no attribute {name!r}")
