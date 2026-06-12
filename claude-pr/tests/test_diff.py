"""Tests for diff.py — parse_diff and map_changes."""

import pytest
from pr_review.diff import parse_diff


# ── parse_diff ────────────────────────────────────────────────────────────────

def test_basic_added_lines():
    diff = """\
diff --git a/foo.py b/foo.py
index 0000000..0000001 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 line1
+added_line
 line2
 line3
"""
    fds = parse_diff(diff)
    assert len(fds) == 1
    assert fds[0].path == "foo.py"
    assert 2 in fds[0].added_lines       # "added_line" is new-side line 2
    assert 1 not in fds[0].added_lines


def test_no_newline_marker_does_not_shift_line_numbers():
    """'\\ No newline at end of file' must NOT advance new_lineno."""
    diff = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 existing
+added_a
\\ No newline at end of file
+added_b
"""
    fds = parse_diff(diff)
    assert len(fds) == 1
    added = fds[0].added_lines
    # added_a is line 2, added_b is line 3 (the backslash line must not advance counter)
    assert 2 in added
    assert 3 in added
    assert 4 not in added


def test_new_file_diff():
    diff = """\
diff --git a/new.py b/new.py
new file mode 100644
index 0000000..abcdef1
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+line_one
+line_two
"""
    fds = parse_diff(diff)
    assert len(fds) == 1
    assert fds[0].is_new is True
    assert fds[0].path == "new.py"
    assert fds[0].added_lines == {1, 2}


def test_deleted_file_diff():
    """Deleted files (+++ /dev/null) have is_deleted=True.
    parse_diff filters out files with no path (path set only from +++ b/... lines),
    so a pure deletion produces no FileDiff. run_review skips deleted files anyway."""
    diff = """\
diff --git a/gone.py b/gone.py
deleted file mode 100644
index abcdef1..0000000
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-line_one
-line_two
"""
    fds = parse_diff(diff)
    # No path is set for deleted files (path comes from +++ b/... only).
    # The filter `[f for f in files if f.path]` removes them, which is correct
    # because run_review skips is_deleted=True files regardless.
    assert len(fds) == 0


def test_multi_hunk_file():
    diff = """\
diff --git a/bar.py b/bar.py
--- a/bar.py
+++ b/bar.py
@@ -1,3 +1,4 @@
 a
+b
 c
 d
@@ -10,3 +11,4 @@
 x
+y
 z
 w
"""
    fds = parse_diff(diff)
    assert len(fds) == 1
    added = fds[0].added_lines
    assert 2 in added   # first hunk "b" is new-side line 2
    assert 12 in added  # second hunk "y" is new-side line 12


def test_renamed_path_uses_b_side():
    diff = """\
diff --git a/old.py b/new_name.py
rename from old.py
rename to new_name.py
--- a/old.py
+++ b/new_name.py
@@ -1,1 +1,2 @@
 existing
+added
"""
    fds = parse_diff(diff)
    assert len(fds) == 1
    assert fds[0].path == "new_name.py"
    assert 2 in fds[0].added_lines
