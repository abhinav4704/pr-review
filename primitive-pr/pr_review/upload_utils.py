from __future__ import annotations

from pathlib import Path
import io
import os
import tempfile
import zipfile
from typing import Iterable, List, Sequence, Tuple


def sanitize_upload_path(raw_path: str) -> str:
    """Normalize user-provided paths and reject unsafe traversal patterns."""
    p = (raw_path or "").replace("\\", "/").strip()
    if not p:
        raise ValueError("Upload path is empty")

    parts = [part for part in p.split("/") if part not in ("", ".")]
    if not parts:
        raise ValueError("Upload path is invalid")

    sanitized: List[str] = []
    for part in parts:
        if part == "..":
            raise ValueError(f"Unsafe upload path traversal: {raw_path}")
        # Reject Windows drive prefixes inside a segment like C: or D:
        if ":" in part:
            raise ValueError(f"Unsafe upload path drive specifier: {raw_path}")
        sanitized.append(part)

    rel = "/".join(sanitized)
    if rel.startswith("/"):
        raise ValueError(f"Unsafe absolute upload path: {raw_path}")
    return rel


def materialize_uploaded_sources(
    direct_files: Sequence[Tuple[str, bytes]],
    zip_bytes: bytes | None,
    allowed_exts: Iterable[str],
) -> Tuple[str, List[str]]:
    """Write uploaded files to a temp source root and return selected code files."""
    src_root = tempfile.mkdtemp(prefix="pr_review_upload_")
    allowed = {ext.lower() for ext in allowed_exts}
    selected: List[str] = []

    if zip_bytes:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                rel = sanitize_upload_path(info.filename)
                ext = os.path.splitext(rel)[1].lower()
                if ext not in allowed:
                    continue
                target = Path(src_root, *rel.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src_fh:
                    target.write_bytes(src_fh.read())
                selected.append(rel)

    for name, blob in direct_files:
        rel = sanitize_upload_path(name)
        ext = os.path.splitext(rel)[1].lower()
        if ext not in allowed:
            continue
        target = Path(src_root, *rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        selected.append(rel)

    selected = sorted(set(selected))
    if not selected:
        raise RuntimeError("No supported code files found in uploaded content.")
    return src_root, selected
