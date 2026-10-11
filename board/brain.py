"""Read-only access to a brain checkout's KB and docs, for agents (AB-22).

A brain is a git checkout holding an LLM-maintained wiki (`kb/wiki` by
default) and directories of other markdown docs. `BOARD_BRAIN_DIR` names the
checkout, and leaving it unset turns the feature off. `BOARD_KB_DIR` is the
wiki inside it and `BOARD_DOC_DIRS` a colon-separated list of doc
directories, both relative to the checkout.

Every operation takes a scope: `kb`, `docs` or `all`. Only `.md` files under
the scope's directories are visible, and a path is judged by where it
resolves, so neither `..` nor a symlink reaches past them. Nothing here
writes.

Search keeps no index. Each query scans the files on disk, which took 11 ms
for 420 files (2.4 MB). A file matches when it contains every query word as
a case-insensitive substring. Files whose title or filename holds more of
the words rank first, then those with more hits in total, then by path.
"""

from __future__ import annotations

import os
from typing import Mapping

from board import core

BRAIN_DIR_ENV = "BOARD_BRAIN_DIR"
KB_DIR_ENV = "BOARD_KB_DIR"
DOC_DIRS_ENV = "BOARD_DOC_DIRS"
DEFAULT_KB_DIR = "kb/wiki"
SCOPES = ("kb", "docs", "all")
MAX_RESULTS = 20
MAX_LINES = 3


class BrainError(core.BoardError):
    """A refused or failed read of the brain."""


def _inside(path: str, root: str) -> bool:
    return os.path.commonpath([path, root]) == root


def _title(text: str, path: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.startswith("title:"):
                title = line[len("title:"):].strip().strip("\"'")
                if title:
                    return title
    for line in lines:
        if line.startswith("# "):
            return line[2:].strip()
    return os.path.splitext(os.path.basename(path))[0]


class Brain:
    def __init__(self, root, kb_dir: str = DEFAULT_KB_DIR, doc_dirs=()):
        self.root = os.path.realpath(root)
        self.kb_dirs = [kb_dir]
        self.doc_dirs = [d for d in doc_dirs if d]

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Brain | None:
        root = env.get(BRAIN_DIR_ENV, "").strip()
        if not root:
            return None
        kb_dir = env.get(KB_DIR_ENV, "").strip() or DEFAULT_KB_DIR
        doc_dirs = [d.strip() for d in env.get(DOC_DIRS_ENV, "").split(":")]
        return cls(root, kb_dir=kb_dir, doc_dirs=doc_dirs)

    def _dirs(self, scope: str) -> list[tuple[str, str]]:
        """(as configured, resolved) for each directory in the scope."""
        if scope not in SCOPES:
            raise BrainError(f"scope is one of {', '.join(SCOPES)}, not {scope!r}")
        named = {"kb": self.kb_dirs, "docs": self.doc_dirs,
                 "all": self.kb_dirs + self.doc_dirs}[scope]
        return [(d, os.path.realpath(os.path.join(self.root, d))) for d in named]

    def _files(self, scope: str):
        """(display path, resolved path) of every visible file, each once."""
        dirs = self._dirs(scope)
        roots = [real for _, real in dirs]
        seen = set()
        for name, real_dir in dirs:
            if not os.path.isdir(real_dir):
                raise BrainError(f"{name} is not a directory under {self.root}")
            for here, subdirs, files in os.walk(real_dir):
                subdirs[:] = sorted(d for d in subdirs if not d.startswith("."))
                for f in files:
                    if not f.endswith(".md"):
                        continue
                    real = os.path.realpath(os.path.join(here, f))
                    if (real in seen or not real.endswith(".md") or not os.path.isfile(real)
                            or not any(_inside(real, r) for r in roots)):
                        continue
                    seen.add(real)
                    yield os.path.relpath(os.path.join(here, f), self.root), real

    @staticmethod
    def _text(real: str) -> str:
        with open(real, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def list(self, scope: str = "all") -> list[dict]:
        """Every visible file's path and title, by path."""
        out = [{"path": p, "title": _title(self._text(real), p)} for p, real in self._files(scope)]
        return sorted(out, key=lambda e: e["path"])

    def read(self, path: str, scope: str = "all") -> dict:
        """One file's path, title and text, if the scope's directories hold it."""
        real = os.path.realpath(os.path.join(self.root, path))
        if not any(_inside(real, d) for _, d in self._dirs(scope)):
            raise BrainError(f"{path} is outside the {scope} directories")
        if not real.endswith(".md"):
            raise BrainError(f"{path} is not a .md file")
        if not os.path.isfile(real):
            raise BrainError(f"no such file: {path}")
        text = self._text(real)
        return {"path": os.path.normpath(path), "title": _title(text, path), "text": text}

    def search(self, q: str, scope: str = "all") -> list[dict]:
        """The best MAX_RESULTS files holding every word of `q`."""
        words = q.lower().split()
        if not words:
            raise BrainError("the query has no words")
        ranked = []
        for path, real in self._files(scope):
            text = self._text(real)
            low = text.lower()
            if not all(w in low for w in words):
                continue
            title = _title(text, path)
            named = (title + " " + os.path.basename(path)).lower()
            lines = [{"line": n, "text": line.strip()}
                     for n, line in enumerate(text.splitlines(), 1)
                     if any(w in line.lower() for w in words)][:MAX_LINES]
            hit = {"path": path, "title": title, "lines": lines}
            key = (-sum(w in named for w in words), -sum(low.count(w) for w in words), path)
            ranked.append((key, hit))
        ranked.sort(key=lambda r: r[0])
        return [hit for _, hit in ranked[:MAX_RESULTS]]
