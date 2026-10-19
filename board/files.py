"""Attachment bytes on disk, shared by the web board and the MCP tools (AB-74).

The bytes go to `BOARD_ATTACHMENT_DIR`, named by their SHA-256, and only the
description `store` returns goes to `board.core`. Each file is capped at
`BOARD_MAX_UPLOAD_MB` (default 25), and every file of a call is checked
against the cap before any is written, so a refused call leaves nothing behind.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import tempfile
import unicodedata
from pathlib import Path

from board import core

ATTACH_DIR_ENV = "BOARD_ATTACHMENT_DIR"
MAX_UPLOAD_ENV = "BOARD_MAX_UPLOAD_MB"
DEFAULT_MAX_UPLOAD_MB = 25


class TooLarge(core.BoardError):
    """A file over the cap, named by `filename`."""

    def __init__(self, filename: str):
        self.filename = filename
        mb = max_upload_bytes() / (1024 * 1024)
        super().__init__(f"{filename} is over the {mb:g} MB limit for one file")


def safe_filename(name: str | None) -> str:
    """A client's filename cut to its last path component, without control characters."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if unicodedata.category(c)[0] != "C").strip()
    return name[:255] if name not in ("", ".", "..") else "file"


def max_upload_bytes() -> int:
    """The cap in bytes. A value that is not a positive finite number means the default."""
    try:
        mb = float(os.environ.get(MAX_UPLOAD_ENV, "").strip() or DEFAULT_MAX_UPLOAD_MB)
    except ValueError:
        mb = DEFAULT_MAX_UPLOAD_MB
    if not (0 < mb < float("inf")):
        mb = DEFAULT_MAX_UPLOAD_MB
    return int(mb * 1024 * 1024)


def attachment_dir() -> Path:
    """BOARD_ATTACHMENT_DIR, refused when unset because the board then takes no files."""
    root = os.environ.get(ATTACH_DIR_ENV, "").strip()
    if not root:
        raise core.BoardError(f"{ATTACH_DIR_ENV} is not set, so this board takes no files")
    return Path(root)


def store(files: list[tuple[str | None, str | None, bytes]]) -> list[dict]:
    """Write (filename, content_type, data) triples and describe them for `core`.

    The same bytes stored twice take one file.
    """
    if not files:
        return []
    root = attachment_dir()
    cap = max_upload_bytes()
    for name, _, data in files:
        if len(data) > cap:
            raise TooLarge(safe_filename(name))
    root.mkdir(parents=True, exist_ok=True)
    described = []
    for name, content_type, data in files:
        digest = hashlib.sha256(data).hexdigest()
        target = root / digest
        if not target.exists():
            fd, tmp = tempfile.mkstemp(dir=root)
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            os.replace(tmp, target)
        described.append({"filename": safe_filename(name),
                          "content_type": (content_type or "application/octet-stream")[:200],
                          "size": len(data), "sha256": digest})
    return described


def store_base64(items: list[dict]) -> list[dict]:
    """`store` for {filename, content_type?, content_base64} items, as MCP sends them.

    A client such as Claude Desktop shares no filesystem with the board, and
    base64 is the one shape every MCP client can send. Text that is not
    base64 is refused, and so is one that decodes past the cap, before any
    file is written.
    """
    if not items:
        return []
    attachment_dir()
    cap = max_upload_bytes()
    decoded = []
    for item in items:
        # A client may wrap the text in lines, as MIME does.
        name, text = item["filename"], "".join(item["content_base64"].split())
        # Four characters carry three bytes, so a longer text cannot fit.
        if len(text) > (cap // 3 + 1) * 4 + 4:
            raise TooLarge(safe_filename(name))
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise core.BoardError(f"{safe_filename(name)!r} is not valid base64") from None
        decoded.append((name, item.get("content_type"), data))
    return store(decoded)
