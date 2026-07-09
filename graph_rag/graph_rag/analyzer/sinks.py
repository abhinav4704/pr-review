"""Sink catalog — loaded at analysis time, not index time.

Moving sink classification out of graph_core/taint.py into this module means:
  - Adding/tweaking sinks never requires re-indexing; run `analyze` again.
  - Repos can ship a custom sinks.json that extends or overrides the built-ins.

Public API:
    load_sinks(repo_root="") -> SinkCatalog
    catalog.classify(recv, name) -> str | None    # returns vuln_class or None
"""
from __future__ import annotations

import json
import os
import re as _re

# Well-known builtins that are terminal wrt taint propagation — the same list
# as dataflow.py's _TAINT_INERT_BUILTINS. Kept in sync rather than imported to
# avoid a cross-package circular dependency (graph_core ← analyzer is one-way).
TAINT_INERT_BUILTINS: frozenset[str] = frozenset({
    "list", "str", "dict", "set", "tuple", "sorted", "len", "int", "float",
    "bool", "repr", "print", "iter", "next", "reversed", "enumerate", "zip",
    "map", "filter", "frozenset", "bytes", "bytearray", "format", "hash",
    "min", "max", "sum", "any", "all", "abs", "round",
})

# Regex that hints a function might sanitize/escape/validate its input.
SANITIZER_HINTS: "_re.Pattern[str]" = _re.compile(
    r"(sanitiz|escape|clean|validate|quote|encode|strip_tags|whitelist|allowlist)", _re.I
)

# Default sink patterns: vuln_class -> tuple of bare call names.
_DEFAULT_SINK_PATTERNS: dict[str, tuple[str, ...]] = {
    "sql_injection": (
        "execute", "executemany", "raw", "rawquery", "executescript",
        "executequery", "executeupdate", "executebatch", "executelargeupdate",
    ),
    "command_injection": (
        "system", "popen", "call", "run", "spawn", "getoutput",
        "check_output", "check_call", "popen2", "popen3", "popen4",
        "exec",
    ),
    "deserialization": ("loads", "load", "unpickle", "yaml_load", "read_pickle"),
    "path_traversal": (
        "open", "sendfile", "send_file", "remove", "unlink", "rmtree",
        "extractall", "read_text", "write_text", "readlink",
    ),
    "eval_injection": ("eval", "exec", "compile"),
    "ssrf": ("get", "post", "put", "delete", "request", "urlopen", "fetch"),
    "template_injection": ("render_template_string", "from_string"),
    "xss": ("mark_safe", "Markup"),
    "open_redirect": ("redirect", "HttpResponseRedirect"),
    "nosql_injection": (
        "find", "find_one", "find_one_and_update", "find_one_and_delete",
        "aggregate", "update_many", "update_one", "delete_many", "delete_one",
        "insert_many",
    ),
    "ldap_injection": ("search_s", "search", "bind_s"),
    "xxe": ("parse", "fromstring", "iterparse"),
    "sensitive_data_exposure": ("debug", "info", "warning", "warn", "error", "exception", "critical"),
    "crypto_misuse": ("encrypt", "decrypt", "new"),
}

# Receiver hints narrow common-word sinks to avoid false positives.
_DEFAULT_RECEIVER_HINTS: dict[str, tuple[str, ...]] = {
    "path_traversal": ("os", "shutil", "", "zipfile", "tarfile", "pathlib"),
    "ssrf": ("requests", "urllib", "http", "httpx", "aiohttp", "session"),
    "nosql_injection": ("collection", "db", "mongo", "mongodb", "client", "coll"),
    "ldap_injection": ("ldap", "conn", "connection", "server"),
    "xxe": ("etree", "lxml", "xml", "sax", "minidom", "elementtree"),
    "sensitive_data_exposure": ("logger", "log", "logging"),
    "crypto_misuse": ("cipher", "aes", "des", "rsa", "crypto", "cryptography"),
}


# Receivers that make an unresolved external call worth surfacing as a finding.
# Bare calls (no receiver) to unresolved names are almost always unindexed
# internal helpers — emitting a finding for them is noise. Calls where the
# receiver is a known external library are genuinely worth flagging even when
# the specific callee method isn't in the built-in sink patterns.
DANGEROUS_EXTERNAL_RECEIVERS: frozenset[str] = frozenset({
    # HTTP / network
    "requests", "urllib", "http", "httpx", "aiohttp", "session", "client",
    # OS / process
    "subprocess", "os", "shutil", "socket", "smtplib", "ftplib",
    # Databases / caches
    "cursor", "conn", "connection", "db", "redis", "mongo", "collection",
    "elasticsearch", "es", "cassandra",
    # Cloud / infra
    "boto3", "s3", "sqs", "sns", "lambda_client",
})


class SinkCatalog:
    """Classifies (recv_tail, call_name) pairs against the configured sink taxonomy."""

    def __init__(self, patterns: dict[str, tuple[str, ...]],
                 receiver_hints: dict[str, tuple[str, ...]]) -> None:
        self._patterns = {k: frozenset(v) for k, v in patterns.items()}
        self._hints = receiver_hints

    def classify(self, recv: str, name: str) -> str | None:
        """Return the vuln_class for (recv, name), or None if not a known sink."""
        name_l = (name or "").lower()
        recv_l = (recv or "").lower()
        for vuln_class, names in self._patterns.items():
            if name_l not in names:
                continue
            hints = self._hints.get(vuln_class)
            if hints is None or recv_l in hints:
                return vuln_class
        return None


def load_sinks(repo_root: str = "") -> SinkCatalog:
    """Build the SinkCatalog, optionally extended/overridden by
    `<repo_root>/sinks.json`. The JSON schema is:
        {
          "patterns": {"vuln_class": ["name", ...]},
          "receiver_hints": {"vuln_class": ["recv_tail", ...]}
        }
    Keys not present in the JSON keep their built-in values.
    """
    patterns = {k: tuple(v) for k, v in _DEFAULT_SINK_PATTERNS.items()}
    hints = {k: tuple(v) for k, v in _DEFAULT_RECEIVER_HINTS.items()}

    if repo_root:
        custom_path = os.path.join(repo_root, "sinks.json")
        if os.path.isfile(custom_path):
            try:
                with open(custom_path, "r", encoding="utf-8") as fh:
                    custom = json.load(fh)
                for k, v in (custom.get("patterns") or {}).items():
                    patterns[k] = tuple(v)
                for k, v in (custom.get("receiver_hints") or {}).items():
                    hints[k] = tuple(v)
            except (OSError, json.JSONDecodeError):
                pass

    return SinkCatalog(patterns, hints)
