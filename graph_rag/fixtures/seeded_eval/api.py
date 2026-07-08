"""HTTP entry points for the seeded eval branch.

`app` is a minimal route-decorator stub (no Flask dependency needed) — the
extractor only parses the `@app.get(...)` AST shape, it never imports/executes
this module, so a stub is enough to produce real EXPOSES edges + the
`endpoint_handler` component_role Stage 1 needs to seed taint sources from.
"""
from .service import run_search
from .shell import run_diagnostic


class _Router:
    def get(self, route):
        def deco(fn):
            return fn
        return deco


app = _Router()


@app.get("/search")
def search_endpoint(query):
    """Seed #1: multi-hop SQL injection — query flows to fetch_rows' sink
    via run_search -> normalize (no-op) -> fetch_rows."""
    return run_search(query)


@app.get("/diagnostics")
def diagnostics_endpoint(host):
    """Seed #2: multi-hop command injection — host flows to os.system via
    run_diagnostic."""
    return run_diagnostic(host)
