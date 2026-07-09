"""Unified finding deduplication for all agents.

Replaces the old two-pass approach (dedupe by id, then
collapse_cross_category_duplicates) with a single function that also handles
related subcategories within the same vulnerability family.

FAMILY maps subcategories to a canonical family key. When a graph_proven
finding exists for (owning_fqn, family), llm_judged findings in the same
family for the same function are suppressed — graph_proven is authoritative.
"""
from __future__ import annotations

from ..graph_core.findings import Finding

FAMILY: dict[str, str] = {
    # SQL / NoSQL — same underlying injection pattern
    "sql_injection": "sqli",
    "nosql_injection": "sqli",
    # Command / eval — OS-level execution
    "command_injection": "cmdi",
    "eval_injection": "cmdi",
    # File system access
    "path_traversal": "path",
    # Network requests
    "ssrf": "net",
    # Output encoding
    "xss": "xss",
    "open_redirect": "redir",
    "template_injection": "template",
    # Deserialization
    "deserialization": "deser",
    # Directory services
    "ldap_injection": "ldap",
    # XML parsing
    "xxe": "xxe",
    # Data exposure / logging
    "sensitive_data_exposure": "data_exposure",
    "sensitive_data_logged": "data_exposure",
    # Cryptography
    "crypto_misuse": "crypto",
    "weak_crypto": "crypto",
    "insecure_randomness": "crypto",
}


def dedupe_findings(findings: list[Finding]) -> list[Finding]:
    """Three-pass deduplication, stable (preserves input order within each pass).

    Pass 1 — exact id dedup (same owning_fqn + category + subcategory + line).
    Pass 2 — collect (owning_fqn, family) keys where source == graph_proven.
    Pass 3 — drop llm_judged findings when a graph_proven one exists in the
              same family for the same function.
    """
    # Pass 1: exact id
    seen_ids: set[str] = set()
    deduped: list[Finding] = []
    for f in findings:
        if f.id in seen_ids:
            continue
        seen_ids.add(f.id)
        deduped.append(f)

    # Pass 2: proven keys
    proven_keys: set[tuple[str, str]] = set()
    for f in deduped:
        if f.source == "graph_proven":
            fam = FAMILY.get(f.subcategory)
            if fam:
                proven_keys.add((f.owning_fqn, fam))

    # Pass 3: suppress llm_judged duplicates within a family
    out: list[Finding] = []
    for f in deduped:
        if f.source != "graph_proven":
            fam = FAMILY.get(f.subcategory)
            if fam and (f.owning_fqn, fam) in proven_keys:
                continue
        out.append(f)
    return out
