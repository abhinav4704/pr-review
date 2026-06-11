"""Parse a unified diff and map changed lines to graph nodes.

Only ADDED lines (lines starting with +) are analyzed — context lines and
removed lines are ignored. This means findings always refer to code that
is actually in the PR, on its new-side line numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .graph import CodeGraph

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileDiff:
    path: str
    is_new: bool
    is_deleted: bool
    added_lines: Set[int]       # new-side line numbers of ADDED (+) lines only
    changed_lines: Set[int]     # alias kept for compatibility (same as added_lines)

    def __post_init__(self):
        self.changed_lines = self.added_lines


def parse_diff(diff_text: str) -> List[FileDiff]:
    """Parse unified diff -> per-file added line sets (+ lines only, new side)."""
    files: List[FileDiff] = []
    cur: Optional[FileDiff] = None
    new_lineno = 0

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            if cur:
                files.append(cur)
            cur = FileDiff(path="", is_new=False, is_deleted=False,
                           added_lines=set(), changed_lines=set())
            new_lineno = 0
            continue
        if cur is None:
            continue

        if line.startswith("new file mode"):
            cur.is_new = True
        elif line.startswith("deleted file mode"):
            cur.is_deleted = True
        elif line.startswith("+++ "):
            target = line[4:].strip()
            if target != "/dev/null":
                cur.path = target[2:] if target.startswith("b/") else target
        elif line.startswith("@@"):
            m = _HUNK.match(line)
            if m:
                new_lineno = int(m.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            # This is an ADDED line — the only lines we analyze
            cur.added_lines.add(new_lineno)
            new_lineno += 1
        elif line.startswith("-") and not line.startswith("---"):
            pass  # removed line; does not advance new-side counter
        else:
            # context line: advances new-side line counter
            if new_lineno:
                new_lineno += 1

    if cur:
        files.append(cur)
    return [f for f in files if f.path]


@dataclass
class ChangedNode:
    node_id: str
    change_type: str    # "added" | "signature" | "behavior"
    lines: List[int]    # added (+) line numbers that fall inside this node
    file_path: str = ""


def map_changes(cg: CodeGraph, file_diffs: List[FileDiff]) -> List[ChangedNode]:
    """Map added lines to innermost graph nodes and classify the change type."""
    by_node: Dict[str, List[int]] = {}
    added_files: Set[str] = set()

    for fd in file_diffs:
        if fd.is_deleted:
            continue
        if fd.is_new:
            added_files.add(fd.path)
        for ln in sorted(fd.added_lines):
            node = cg.node_for_line(fd.path, ln)
            if node:
                by_node.setdefault(node, []).append(ln)

    out: List[ChangedNode] = []
    for node_id, lines in by_node.items():
        d = cg.node(node_id)
        if d["path"] in added_files:
            ctype = "added"
        elif d["start_line"] in lines:
            ctype = "signature"
        else:
            ctype = "behavior"
        out.append(ChangedNode(
            node_id=node_id,
            change_type=ctype,
            lines=sorted(lines),
            file_path=d["path"],
        ))

    priority = {"signature": 0, "added": 1, "behavior": 2}
    out.sort(key=lambda c: (priority.get(c.change_type, 9), -cg.fan_in(c.node_id)))
    return out


def group_by_file(changes: List[ChangedNode]) -> Dict[str, List[ChangedNode]]:
    """Group ChangedNode list by file path."""
    out: Dict[str, List[ChangedNode]] = {}
    for ch in changes:
        out.setdefault(ch.file_path, []).append(ch)
    return out
