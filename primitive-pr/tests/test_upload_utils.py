from __future__ import annotations

from io import BytesIO
import zipfile

import pytest

from pr_review.upload_utils import materialize_uploaded_sources


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def test_materialize_direct_files_filters_extensions():
    src_root, files = materialize_uploaded_sources(
        direct_files=[
            ("a.py", b"def a():\n    return 1\n"),
            ("notes.txt", b"ignore"),
            ("pkg/b.ts", b"export const b = 1;\n"),
        ],
        zip_bytes=None,
        allowed_exts={".py", ".ts"},
    )

    assert src_root
    assert files == ["a.py", "pkg/b.ts"]


def test_materialize_zip_with_safe_paths():
    blob = _zip_bytes(
        {
            "src/main.py": b"def main():\n    pass\n",
            "src/ignore.md": b"# n/a\n",
            "sub/util.js": b"export function util() {}\n",
        }
    )

    src_root, files = materialize_uploaded_sources(
        direct_files=[],
        zip_bytes=blob,
        allowed_exts={".py", ".js"},
    )

    assert src_root
    assert files == ["src/main.py", "sub/util.js"]


def test_materialize_zip_rejects_path_traversal():
    blob = _zip_bytes({"../evil.py": b"print('x')\n"})

    with pytest.raises(ValueError):
        materialize_uploaded_sources(
            direct_files=[],
            zip_bytes=blob,
            allowed_exts={".py"},
        )
