"""Stage 1 — SQL DDL extractor (regex-based, no tree-sitter).

Emits:
    Nodes: Table — one per CREATE TABLE statement.
           Properties include columns_json (JSON list of {name, type} dicts),
           start_line, file, fqn = "{repo}.{table_name}".
    Edges (resolved): REFERENCES — FK constraint: source Table -> target Table.

No RawRefs emitted — all Table->Table edges are within the same file and fully
resolved by name at extraction time.
"""
from __future__ import annotations

import json
import re

from ..discovery import FileInfo
from ..ids import make_id
from ..models import Confidence, Edge, Node, Origin

EXTRACTOR = "sql-ddl"

# CREATE [OR REPLACE] [TEMP|TEMPORARY] TABLE [IF NOT EXISTS] [schema.]name (...)
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?TABLE\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:`?\"?(\w+)`?\"?\.)?"   # optional schema prefix
    r"`?\"?(\w+)`?\"?\s*\(",    # table name
    re.I,
)

# FOREIGN KEY (...) REFERENCES [schema.]table (...)
_FK_RE = re.compile(
    r"REFERENCES\s+(?:`?\"?(\w+)`?\"?\.)?"   # optional schema
    r"`?\"?(\w+)`?\"?",                        # referenced table name
    re.I,
)

# Column definition: name type[(size)] [constraints...]
# Only matches lines that start with an identifier followed by a type keyword.
_COL_RE = re.compile(
    r"^\s*`?\"?(\w+)`?\"?\s+"
    r"(BIGINT|BINARY|BIT|BLOB|BOOL(?:EAN)?|CHAR|CLOB|DATE(?:TIME(?:OFFSET)?)?|"
    r"DECIMAL|DOUBLE|ENUM|FLOAT|INT(?:EGER)?|JSON|LONGTEXT|MEDIUMTEXT|MONEY|"
    r"NCHAR|NTEXT|NUMBER|NUMERIC|NVARCHAR|REAL|SET|SMALLINT|TEXT|TIME(?:STAMP)?|"
    r"TINYINT|UNIQUEIDENTIFIER|UUID|VARBINARY|VARCHAR|XMLTYPE)\b",
    re.I | re.M,
)

# Lines that are constraints, not column defs — skip them for column parsing.
_CONSTRAINT_RE = re.compile(
    r"^\s*(?:CONSTRAINT|PRIMARY\s+KEY|UNIQUE|CHECK|FOREIGN\s+KEY|KEY\b|INDEX\b)",
    re.I | re.M,
)


def _parse_columns(body: str) -> list[dict]:
    """Parse column definitions out of a CREATE TABLE body string."""
    cols = []
    seen: set[str] = set()
    for line in body.splitlines():
        if _CONSTRAINT_RE.match(line):
            continue
        m = _COL_RE.match(line)
        if m:
            cname = m.group(1).lower()
            if cname not in seen:
                seen.add(cname)
                cols.append({"name": cname, "type": m.group(2).upper()})
    return cols


def _find_tables(src: str) -> list[tuple[str, int, str, str]]:
    """Return list of (table_name, line_number, schema_or_empty, body_str)."""
    results = []
    for m in _CREATE_TABLE_RE.finditer(src):
        schema = (m.group(1) or "").lower()
        name = m.group(2).lower()
        start_pos = m.end()
        line_no = src[:m.start()].count("\n") + 1

        # Find matching closing paren for the table body
        depth = 1
        i = start_pos
        while i < len(src) and depth > 0:
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
            i += 1
        body = src[start_pos : i - 1]
        results.append((name, line_no, schema, body))
    return results


def extract(file: FileInfo, repo: str):
    """Return (nodes, edges, refs) for a .sql file."""
    src = file.source.decode("utf-8", "replace")

    nodes: list[Node] = []
    edges: list[Edge] = []
    table_id_by_name: dict[str, str] = {}

    for table_name, line_no, schema, body in _find_tables(src):
        fqn_parts = [repo]
        if schema:
            fqn_parts.append(schema)
        fqn_parts.append(table_name)
        fqn = ".".join(fqn_parts)

        node_id = make_id(repo, fqn, "Table")
        table_id_by_name[table_name] = node_id

        columns = _parse_columns(body)

        nodes.append(
            Node(
                id=node_id,
                label="Table",
                name=table_name,
                fqn=fqn,
                repo=repo,
                kind="table",
                file=file.relpath,
                package=schema,
                start_line=line_no,
                body_hash=make_id(repo, body, "body"),  # stable id for change detection
                extractor=EXTRACTOR,
                confidence=Confidence.EXTRACTED.value,
                # store columns as a JSON string in docstring slot so it reaches Neo4j
                # without adding a new model field.  Prefixed so queries can detect it.
                docstring=json.dumps(columns) if columns else "",
            )
        )

    # FK REFERENCES — only emit when both tables are defined in this file or
    # carry forward as RawRef candidates.  For simplicity, emit resolved edges
    # for same-file references and skip cross-file FK resolution (the resolver
    # will handle them via name-match the same way it handles CALLS).
    src_stripped = re.sub(r"--[^\n]*", "", src)  # remove line comments
    for m in _FK_RE.finditer(src_stripped):
        ref_table = m.group(2).lower()
        ref_id = table_id_by_name.get(ref_table)
        if ref_id is None:
            continue  # cross-file FK — resolver handles later

        # Find which table this FK belongs to by walking backwards in src
        pre = src_stripped[: m.start()]
        # Find the last CREATE TABLE before this REFERENCES keyword
        last_ct = None
        for ct in _CREATE_TABLE_RE.finditer(pre):
            last_ct = ct
        if last_ct is None:
            continue
        src_table = last_ct.group(2).lower()
        src_id = table_id_by_name.get(src_table)
        if src_id is None:
            continue

        line_no = src[: m.start()].count("\n") + 1
        edges.append(
            Edge(
                type="REFERENCES",
                src=src_id,
                dst=ref_id,
                confidence=Confidence.EXTRACTED.value,
                origin=Origin.EXTRACTED.value,
                extractor=EXTRACTOR,
                evidence_file=file.relpath,
                evidence_line=line_no,
            )
        )

    return nodes, edges, []  # no RawRefs
