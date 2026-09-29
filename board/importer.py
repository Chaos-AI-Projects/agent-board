"""`board import-backlog`: brain's markdown backlog into the board (design section 9).

The files are read through brain's own `backlog.py`, loaded from its path,
so the board and `backlog.py next` cannot disagree on what a file says. Each
file becomes a project keyed by its id prefix, each item an issue under its
own id, and each `- notes:` line an annotate event.

Two choices make `board next` pick what `backlog.py next` picks (PRD section
9 criterion 6):

- **Rank is file order, with `in-progress` items first.** `backlog.py`
  takes any `in-progress` item before any `ready` one, where the board takes
  the lowest rank. Every other state keeps file order, so an `open` item
  made `ready` later still sits where its file put it.
- **An `in-progress` item arrives under an expired lease.** In `backlog.py`
  it means a run checkpointed and died, which on the board is a lease that
  ran out, and `next` reclaims exactly that.

Each backlog state lands in its board lane through `LANES`: the board
renamed four of them (MS-632) and `backlog.py` did not.

The import is one transaction and runs once: a project that already exists
refuses the whole import.
"""

from __future__ import annotations

import importlib.util
import secrets
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from board import store
from board.core import BoardError, _event, new_bucket
from board.store import Issue, Project

DEFAULT_BACKLOG_PY = "/home/overlord/brain/backlog.py"

LANES = {"open": "backlog", "ready": "ready", "in-progress": "processing",
         "blocked": "need-input", "frozen": "onhold", "done": "done"}


def load_backlog_module(path: str | Path = DEFAULT_BACKLOG_PY):
    path = Path(path)
    if not path.is_file():
        raise BoardError(f"no backlog.py at {path}")
    spec = importlib.util.spec_from_file_location("brain_backlog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def import_backlog(engine, backlog, root: str | Path | None = None, *,
                   actor: str, actor_kind: str) -> dict:
    """Import every file `backlog.load_items` reads under `root`."""
    root = Path(root if root is not None else backlog.BACKLOG_DIR)
    items = backlog.load_items(root)

    files: dict[str, list[dict]] = {}
    for it in items:
        files.setdefault(it["file"], []).append(it)
    projects = {name: _prefix(name, its) for name, its in files.items()}

    def pick_rank(pair):
        position, it = pair
        return (it["state"] != "in-progress", position)

    ranks = {it["id"]: 10 * n
             for n, (_, it) in enumerate(sorted(enumerate(items), key=pick_rank), 1)}

    with store.session(engine) as s, s.begin():
        taken = s.scalars(select(Project.key).where(Project.key.in_(projects.values()))).all()
        if taken:
            raise BoardError(f"project {', '.join(sorted(taken))} already exists; "
                             "import-backlog runs once")
        now = store.db_now(s)
        for name, its in files.items():
            key = projects[name]
            lines = (root / name).read_text().splitlines()
            s.add(Project(key=key, name=Path(name).stem, colour_bucket=new_bucket(s, key),
                          next_number=max(_number(it) for it in its) + 1))
            s.flush()
            for it in its:
                _add_issue(s, backlog, it, key, ranks[it["id"]], lines, now,
                           actor, actor_kind)

    return {"root": str(root),
            "projects": [{"key": projects[name], "file": name, "issues": len(its)}
                         for name, its in files.items()],
            "issues": len(items)}


def _add_issue(s, backlog, it, key, rank, lines, now, actor, actor_kind):
    state = it["state"]
    if state not in backlog.STATES or state not in LANES:
        raise BoardError(f"{it['id']} in {it['file']}: state {state!r} is not one of "
                         f"{backlog.STATES}")
    issue = Issue(id=it["id"], project_key=key, title=it["title"],
                  body=f"origin: {it['origin']}" if it["origin"] else "",
                  state=LANES[state], rank=rank, created_at=now, updated_at=now)
    if state == "in-progress":
        issue.lease_holder = actor
        issue.lease_token = secrets.token_hex(16)
        issue.lease_expires_at = now - timedelta(seconds=1)
    s.add(issue)
    s.flush()
    _event(s, issue.id, now, actor, actor_kind, "create", None, LANES[state],
           f"imported from backlog/{it['file']}", None)
    for note in _notes(backlog, lines, it):
        _event(s, issue.id, now, actor, actor_kind, "annotate", None, None, note, None)


def _notes(backlog, lines, it):
    """Every `- notes:` line of one item, where the parser keeps only the last.

    Walks the item's own line span with the parser's regex, fence and
    continuation rules, then checks the last note against the parser's value, so a drift
    between the two readings fails the import instead of passing silently.
    """
    notes, field, in_fence = [], None, False
    for line in lines[it["heading_line"]:it["end_line"]]:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        matched = backlog.FIELD_RE.match(line)
        if matched and matched.group(1) in backlog.FIELDS:
            field = matched.group(1)
            if field == "notes":
                notes.append(matched.group(2).strip())
        elif field and line[:1] in (" ", "\t") and line.strip():
            if field == "notes":
                notes[-1] = f"{notes[-1]} {line.strip()}".strip()
        elif not line.strip():
            field = None
    if (notes[-1] if notes else "") != it["notes"]:
        raise BoardError(f"{it['id']}: read its last note as {notes[-1:]!r}, "
                         f"backlog.py reads {it['notes']!r}")
    return [n for n in notes if n]


def _prefix(name, its):
    prefixes = {it["id"].partition("-")[0] for it in its}
    if len(prefixes) != 1:
        raise BoardError(f"{name} mixes id prefixes {sorted(prefixes)}; "
                         "a file becomes one project")
    return prefixes.pop()


def _number(it):
    prefix, _, number = it["id"].partition("-")
    if not number.isdigit():
        raise BoardError(f"{it['id']} in {it['file']} is not <PREFIX>-<number>")
    return int(number)
