"""Shared helpers for the HTTP-API layer (Endpoint nodes + CALLS_API edges).

Deterministic, framework-agnostic primitives used by both the language
extractors (which detect route definitions and outbound HTTP calls) and the
resolver (which matches a call's URL to an exposed Endpoint).

The matching contract is a normalized `(METHOD, route)` key:
  - METHOD is upper-case (GET/POST/...).
  - route is the URL path with path-parameters collapsed to `*`, so that a
    caller's concrete `/api/nodes/123` matches a server's templated
    `/api/nodes/{node_id}` (FastAPI), `/api/nodes/<id>` (Flask) or
    `/api/nodes/:id` (Express).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .ids import make_id

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}

# Receiver roots that denote an HTTP client (outbound call detection, Python).
HTTP_CLIENT_RECEIVERS = {
    "requests", "httpx", "session", "client", "http", "aiohttp", "urllib3", "session_",
}

# Path-parameter shapes: {id} / {id:int} (FastAPI/Starlette/Flask 2),
# <id> / <int:id> (Flask/Werkzeug), :id (Express/rails).
_PARAM_BRACE = re.compile(r"\{[^/}]*\}")
_PARAM_ANGLE = re.compile(r"<[^/>]*>")
_PARAM_COLON = re.compile(r":[^/]+")


def split_url(url: str) -> tuple[str, str]:
    """Return (host, path). Relative URLs yield ('', path)."""
    url = (url or "").strip().strip("'\"")
    if "://" in url or url.startswith("//"):
        parts = urlsplit(url if "://" in url else "http:" + url)
        return parts.netloc.lower(), parts.path or "/"
    # strip any query/fragment from a relative path
    return "", urlsplit(url).path or url


def normalize_route(route: str) -> str:
    """Collapse path params to `*`, drop trailing slash, ensure leading `/`."""
    route = (route or "").strip().strip("'\"")
    if not route:
        return ""
    route = _PARAM_BRACE.sub("*", route)
    route = _PARAM_ANGLE.sub("*", route)
    route = _PARAM_COLON.sub("*", route)
    if not route.startswith("/"):
        route = "/" + route
    if len(route) > 1 and route.endswith("/"):
        route = route.rstrip("/")
    return route


def match_key(method: str, route: str) -> tuple[str, str]:
    return (method or "GET").upper(), normalize_route(route)


def endpoint_display(method: str, route: str, host: str = "") -> str:
    return f"{(method or 'GET').upper()} {host}{normalize_route(route)}".strip()


def endpoint_fqn(method: str, route: str, host: str = "") -> str:
    return f"{(method or 'GET').upper()} {host}{normalize_route(route)}"


def endpoint_id(repo: str, method: str, route: str, host: str = "") -> str:
    return make_id(repo, endpoint_fqn(method, route, host), "endpoint")
