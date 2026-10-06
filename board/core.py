"""The board's operations and rules (design section 1).

Every operation is one function, one transaction, and at least one event
row, except `heartbeat`, which moves a lease without changing state and so
writes no event (design section 5), and an `edit` whose form changed nothing. Results are plain dicts, ready for the
CLI to print as JSON.

Actors come in three kinds: "agent", "human" and "system". An agent write
to a leased issue must carry the current lease token, and a stale one raises
`LeaseLost`. A human change under someone else's lease raises `LeaseHeld`
unless the caller preempts; a human note never needs a lease.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import timedelta

from sqlalchemy import and_, exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from board import store
from board.store import (Artifact, Attachment, Dependency, Event, Issue, Label, Project, Template,
                         TemplateStep, Workflow)

DEFAULT_TTL = timedelta(minutes=90)

# Design section 3, under the lane names of MS-632, in board column order.
# `processing` is entered only by `next`, and a lifted hold returns to
# `backlog`, so a human must authorize the item again. An answer can finish
# the work outright, so `need-input` reaches `done` too (AB-8). A human
# closes every card that is not a workflow step: an agent finishes a
# standalone card or a plan origin at `need-input` (AB-9).
TRANSITIONS: dict[str, set[str]] = {
    "backlog": {"ready", "onhold", "cancelled"},
    "ready": {"onhold", "cancelled"},
    "need-input": {"ready", "onhold", "done", "cancelled"},
    "processing": {"done", "need-input", "ready"},
    "onhold": {"backlog"},
    "done": set(),
    "cancelled": set(),
}
NOTE_REQUIRED = {"done", "need-input"}
# One line per lane, shown under its column heading and beside an issue's
# state, so the board explains itself. Keys match TRANSITIONS, in its order.
LANE_HINTS: dict[str, str] = {
    "backlog": "Filed, not authorized. Agents never pick it.",
    "ready": "Authorized. board next takes the highest-ranked card here.",
    "need-input": "An agent is waiting on you: a question, or finished work to review. "
                  "Answer in a note, then drag the card to ready, or to done if "
                  "nothing is left.",
    "processing": "An agent holds it under a 90-minute lease. Only board next puts "
                  "a card here.",
    "onhold": "Frozen; exits only to backlog. A card split into a workflow waits here "
              "and returns to ready when the last step is done.",
    "done": "Finished, with a required note such as a PR. Final. Only a human closes a "
            "card here, except a workflow step, which its agent closes.",
    "cancelled": "Dropped. Final.",
}
CREATE_STATES = {"backlog", "ready"}
ARTIFACT_KINDS = {"commit", "pr", "path", "url"}
ACTOR_KINDS = {"agent", "human", "system"}
# Matches the width of store.Project's columns.
PROJECT_KEY = re.compile(r"[A-Z][A-Z0-9]{0,15}")
PROJECT_NAME_MAX = 200


class BoardError(Exception):
    pass


class NotFound(BoardError):
    pass


class InvalidTransition(BoardError):
    pass


class NoteRequired(BoardError):
    pass


class LeaseLost(BoardError):
    """The caller's lease token is no longer current. CLI exit 4."""


class Conflict(BoardError):
    """The row moved on since the caller read it. CLI exit 5."""


class LeaseHeld(Conflict):
    """Someone else holds the lease; pass preempt=True to take it over."""

    def __init__(self, holder: str, expires_at):
        self.holder = holder
        self.expires_at = expires_at
        until = expires_at.isoformat() if expires_at else "released"
        super().__init__(f"held by {holder} until {until}")


# --- setup helpers ------------------------------------------------------------


def create_project(engine, key: str, name: str) -> dict:
    key, name = key.strip(), _project_name(name)
    if not PROJECT_KEY.fullmatch(key):
        # The key is a path segment in every issue URL, so a `/` or `..` would
        # leave a project no page can reach.
        raise BoardError(f"a project key is 1-16 of A-Z and 0-9, starting with a letter; "
                         f"not {key!r}")
    try:
        with store.session(engine) as s, s.begin():
            s.add(Project(key=key, name=name, colour_bucket=new_bucket(s, key)))
    except IntegrityError:
        raise Conflict(f"project {key!r} already exists") from None
    return {"key": key, "name": name}


# Card colours come from BUCKETS hues, 360/BUCKETS degrees apart (MS-655). The
# first BUCKETS projects each take the lowest empty bucket; after that a
# project's bucket depends only on its key, never on what came before it.
BUCKETS = 10


def key_bucket(key: str) -> int:
    """The bucket a key hashes to. sha256, because hash() is salted per process."""
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big") % BUCKETS


def new_bucket(s, key: str) -> int:
    """The bucket a project created now in session `s` takes."""
    held = set(s.scalars(select(Project.colour_bucket)))
    empty = [b for b in range(BUCKETS) if b not in held]
    return empty[0] if empty else key_bucket(key)


def colour_buckets(engine) -> dict[str, int]:
    """Every project's stored bucket, by key."""
    with store.session(engine) as s:
        return dict(s.execute(select(Project.key, Project.colour_bucket)).all())


def projects(engine) -> list[dict]:
    """Every project in key order, with how many issues it holds."""
    count = (select(func.count()).where(Issue.project_key == Project.key)
             .correlate(Project).scalar_subquery())
    with store.session(engine) as s:
        rows = s.execute(select(Project.key, Project.name, count).order_by(Project.key))
        return [{"key": k, "name": n, "issues": c} for k, n, c in rows]


def rename_project(engine, key: str, name: str) -> dict:
    """A new name for a project. The key cannot change: every issue id carries it."""
    name = _project_name(name)
    with store.session(engine) as s, s.begin():
        project = s.get(Project, key)
        if project is None:
            raise NotFound(f"no project {key!r}")
        project.name = name
    return {"key": key, "name": name}


def delete_project(engine, key: str) -> None:
    """Remove a project with no issues; one with issues is refused."""
    try:
        with store.session(engine) as s, s.begin():
            project = s.get(Project, key, with_for_update=True)
            if project is None:
                raise NotFound(f"no project {key!r}")
            held = s.scalar(select(func.count()).where(Issue.project_key == key))
            if held:
                raise BoardError(f"project {key!r} still has {held} "
                                 f"issue{'' if held == 1 else 's'}; only an empty one can go")
            s.delete(project)
    except IntegrityError:
        # An issue created between the count and the delete; the foreign key refuses it.
        raise BoardError(f"project {key!r} gained an issue; only an empty one can go") from None


def _project_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise BoardError("a project needs a name")
    if len(name) > PROJECT_NAME_MAX:
        raise BoardError(f"a project name is at most {PROJECT_NAME_MAX} characters")
    return name


def create_template(engine, name: str, title: str, steps: list[str]) -> dict:
    with store.session(engine) as s, s.begin():
        s.add(Template(
            name=name,
            title=title,
            steps=[TemplateStep(position=i, title=t) for i, t in enumerate(steps, 1)],
        ))
    return {"name": name, "title": title, "steps": steps}


def create_workflow(engine, title: str) -> int:
    with store.session(engine) as s, s.begin():
        wf = Workflow(title=title, created_at=store.db_now(s))
        s.add(wf)
        s.flush()
        return wf.id


# --- the operations -----------------------------------------------------------


def next(engine, worker: str, *, ttl: timedelta = DEFAULT_TTL, project: str | None = None,
         request_id: str | None = None) -> dict | None:
    """Claim the first workable issue, or return None on an empty queue."""

    def replay(s, ev):
        issue = s.get(Issue, ev.issue_id)
        if issue.lease_holder != worker:
            raise LeaseLost(_lost_message(s, issue))
        return {"issue": _view(s, issue), "lease_token": issue.lease_token}

    def body(s):
        now = store.db_now(s)
        # Bounded: each miss means another caller committed a claim, and
        # there are finitely many candidates.
        for _ in range(100):
            issue = s.scalars(_candidates(now, project)).first()
            if issue is None:
                return None
            prev_holder, prev_state = issue.lease_holder, issue.state
            token = secrets.token_hex(16)
            won = s.execute(
                update(Issue)
                .where(Issue.id == issue.id, Issue.version == issue.version)
                .values(state="processing", lease_holder=worker, lease_token=token,
                        lease_expires_at=now + ttl, version=Issue.version + 1,
                        updated_at=now)
                .execution_options(synchronize_session=False)
            ).rowcount
            if won != 1:
                s.expire(issue)
                continue
            if prev_holder is not None:
                kind, note = "reclaim", f"lease expired; preempted holder {prev_holder}"
            else:
                kind, note = "claim", None
            _event(s, issue.id, now, worker, "agent", kind, prev_state, "processing",
                   note, key)
            s.expire(issue)
            return {"issue": _view(s, issue), "lease_token": token}
        raise Conflict("gave up after 100 lost claim races")

    key = _scoped("next", worker, request_id)
    return _write(engine, key, body, replay, [project, ttl.total_seconds()])


def show(engine, issue_id: str) -> dict:
    with store.session(engine) as s:
        return _view(s, _get(s, issue_id))


def now(engine) -> str:
    """The database clock, which is what a lease expires by."""
    with store.session(engine) as s:
        return _iso(store.db_now(s))


def overview(engine, *, q: str | None = None, project: str | None = None,
             label: str | None = None, assignee: str | None = None) -> dict:
    """Everything the web board renders, read in one session.

    `now` is the database clock, so the board judges a lease expired by the
    same clock `next` uses to reclaim it. Issues carry no events or token.
    The filters are `search`'s; `labels` and `assignees` are every value in
    use, for the filter dropdowns, whatever the filters matched.
    """
    with store.session(engine) as s:
        return {
            "now": _iso(store.db_now(s)),
            "projects": [{"key": p.key, "name": p.name}
                         for p in s.scalars(select(Project).order_by(Project.key))],
            "labels": list(s.scalars(select(Label.name).distinct().order_by(Label.name))),
            "assignees": sorted({a for col in (Issue.assignee, Issue.lease_holder)
                                 for a in s.scalars(select(col).distinct()
                                                    .where(col.is_not(None)))}),
            "templates": [{"name": t.name, "title": t.title}
                          for t in s.scalars(select(Template).order_by(Template.name))],
            "issues": _search(s, q, project, label, assignee),
            "workflows": [_workflow_view(s, wf)
                          for wf in s.scalars(select(Workflow).order_by(Workflow.id))],
        }


def workflow(engine, workflow_id: int) -> dict:
    """One workflow with its steps as full cards, in position order.

    `current` is the first step not yet done, None once every step is.
    `now` is the database clock, as in `overview`, for judging leases.
    """
    with store.session(engine) as s:
        wf = s.get(Workflow, workflow_id)
        if wf is None:
            raise NotFound(f"no workflow {workflow_id!r}")
        view = _workflow_view(s, wf)
        # Not the builtin `next`: this module's `next` is the claim.
        open_steps = [st["id"] for st in view["steps"] if st["state"] != "done"]
        return view | {
            "now": _iso(store.db_now(s)),
            "current": open_steps[0] if open_steps else None,
            "steps": [_row(i) for i in wf.steps],
        }


def search(engine, *, q: str | None = None, project: str | None = None,
           label: str | None = None, assignee: str | None = None) -> list[dict]:
    """Issues matching every filter given, in board order.

    `q` is a case-insensitive substring of the id, title, body or any label,
    matched with plain `lower() LIKE` so the schema needs no full-text
    extension on either engine. `%` and `_` in it are literal characters.
    `project`, `label` and `assignee` match exactly. Nothing writes an
    issue's `assignee` yet, so `assignee` also matches the lease holder: the
    agent or human holding a card is the one working it. An empty filter is
    no filter.
    """
    with store.session(engine) as s:
        return _search(s, q, project, label, assignee)


def _search(s, q, project, label, assignee):
    stmt = select(Issue).order_by(Issue.rank, Issue.id)
    q = (q or "").strip()
    if q:
        # Folded by the database on both sides: SQLite's lower() folds only
        # ASCII, and folding the pattern in Python would miss exact-case text.
        pattern = func.lower("%" + _like_escape(q) + "%")

        def hit(col):
            return func.lower(col).like(pattern, escape="\\")

        stmt = stmt.where(or_(
            hit(Issue.id), hit(Issue.title), hit(Issue.body),
            exists().where(Label.issue_id == Issue.id, hit(Label.name))))
    if project:
        stmt = stmt.where(Issue.project_key == project)
    if label:
        stmt = stmt.where(exists().where(Label.issue_id == Issue.id, Label.name == label))
    if assignee:
        stmt = stmt.where(or_(Issue.assignee == assignee, Issue.lease_holder == assignee))
    return [_row(i) for i in s.scalars(stmt)]


def _row(i):
    """An issue as a board card: no body, events or artifacts."""
    return {
        "id": i.id, "project": i.project_key, "title": i.title, "state": i.state,
        "rank": i.rank, "labels": [l.name for l in i.labels], "assignee": i.assignee,
        "lease_holder": i.lease_holder, "lease_expires_at": _iso(i.lease_expires_at),
        "version": i.version, "workflow_id": i.workflow_id, "position": i.position,
    }


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def transition(engine, issue_id: str, state: str, *, actor: str, actor_kind: str,
               note: str | None = None, token: str | None = None,
               request_id: str | None = None, preempt: bool = False) -> dict:
    """Move an issue. Repeating the current state records the call and changes nothing."""
    key = (_scoped("transition", issue_id, request_id)
           or _derive(actor, "transition", issue_id, token, state, note))

    def body(s):
        issue = _get(s, issue_id, lock=True)
        now = store.db_now(s)
        if actor_kind == "agent" and issue.lease_holder is None:
            raise LeaseLost(f"{issue.id}: an agent moves an issue only under a lease")
        _guard(s, issue, actor, actor_kind, token, now, preempt, None)
        _apply_state(s, issue, state, note, actor, actor_kind, now, key, key_used=False)
        return _view(s, issue)

    return _write(engine, key, body, _replay_view,
                  [state, note, actor, actor_kind, token, preempt])


def annotate(engine, issue_id: str, note: str, *, actor: str, actor_kind: str,
             token: str | None = None, request_id: str | None = None,
             attachments: list[dict] = ()) -> dict:
    """Append a note. A human note needs no lease; it cannot overwrite anything.

    `attachments` are files already stored on disk, as `_attach` describes;
    they hang off this note's event.
    """
    key = (_scoped("annotate", issue_id, request_id)
           or _derive(actor, "annotate", issue_id, token, note, *_digests(attachments)))

    def body(s):
        issue = _get(s, issue_id, lock=True)
        now = store.db_now(s)
        if actor_kind == "agent":
            _check_token(s, issue, actor, token)
        ev = _event(s, issue.id, now, actor, actor_kind, "annotate", None, None, note, key)
        _attach(s, issue, ev.id, attachments, actor, now)
        return _view(s, issue)

    return _write(engine, key, body, _replay_view,
                  [note, actor, actor_kind, token, *_digests(attachments)])


def attachment(engine, attachment_id: int) -> dict:
    """One attachment's metadata, `sha256` included so the caller can find the bytes."""
    with store.session(engine) as s:
        a = s.get(Attachment, attachment_id)
        if a is None:
            raise NotFound(f"no attachment {attachment_id}")
        return _attachment_view(a) | {"sha256": a.sha256, "issue_id": a.issue_id}


def link(engine, issue_id: str, ref: str, *, kind: str, actor: str, actor_kind: str,
         closes: bool = False, token: str | None = None,
         request_id: str | None = None) -> dict:
    """Attach a commit, PR, path or URL. `closes` marks it as finishing the issue."""
    if kind not in ARTIFACT_KINDS:
        raise BoardError(f"artifact kind {kind!r} is not one of {sorted(ARTIFACT_KINDS)}")
    key = (_scoped("link", issue_id, request_id)
           or _derive(actor, "link", issue_id, token, kind, ref, closes))

    def body(s):
        issue = _get(s, issue_id, lock=True)
        now = store.db_now(s)
        if actor_kind == "agent" and issue.lease_holder is None:
            raise LeaseLost(f"{issue.id}: an agent links an artifact only under a lease")
        _guard(s, issue, actor, actor_kind, token, now, False, key)
        s.add(Artifact(issue_id=issue.id, kind=kind, ref=ref, closes=closes,
                       added_at=now, added_by=actor))
        issue.version += 1
        issue.updated_at = now
        note = f"{kind} {ref}" + (" (closes)" if closes else "")
        _event(s, issue.id, now, actor, actor_kind, "link", None, None, note, key)
        return _view(s, issue)

    return _write(engine, key, body, _replay_view,
                  [kind, ref, closes, actor, actor_kind, token])


def create(engine, project: str, title: str, *, actor: str, actor_kind: str,
           body: str = "", state: str = "backlog", rank: int | None = None,
           labels: list[str] = (), workflow_id: int | None = None,
           position: int | None = None, request_id: str | None = None,
           attachments: list[dict] = ()) -> dict:
    if state not in CREATE_STATES:
        raise InvalidTransition(f"an issue is created {sorted(CREATE_STATES)}, not {state!r}")
    if (workflow_id is None) != (position is None):
        raise BoardError("workflow_id and position go together")

    key = _scoped("create", project, request_id)

    def run(s):
        now = store.db_now(s)
        issue = _create(s, project, title, actor, actor_kind, body, state, rank,
                        labels, workflow_id, position, now, key)
        _attach(s, issue, None, attachments, actor, now)
        return _view(s, issue)

    return _write(engine, key, run, _replay_view,
                  [title, body, state, rank, sorted(set(labels)), workflow_id, position,
                   actor, actor_kind, *_digests(attachments)])


def instantiate(engine, template: str, project: str, *, actor: str, actor_kind: str,
                title: str | None = None, request_id: str | None = None) -> dict:
    """Create a workflow and one ready issue per template step, all or nothing."""

    key = _scoped("instantiate", template, request_id)

    def replay(s, ev):
        return _workflow_view(s, s.get(Issue, ev.issue_id).workflow)

    def run(s):
        tpl = s.scalar(select(Template).where(Template.name == template))
        if tpl is None:
            raise NotFound(f"no template {template!r}")
        if not tpl.steps:
            raise BoardError(f"template {template!r} has no steps")
        now = store.db_now(s)
        wf = Workflow(title=title or tpl.title, template_id=tpl.id, created_at=now)
        s.add(wf)
        s.flush()
        for i, step in enumerate(tpl.steps):
            _create(s, project, step.title, actor, actor_kind, step.body, "ready", None,
                    (), wf.id, step.position, now, key if i == 0 else None)
        s.flush()
        s.refresh(wf)
        return _workflow_view(s, wf)

    return _write(engine, key, run, replay, [project, title, actor, actor_kind])


def create_batch(engine, items: list[dict], *, actor: str, actor_kind: str,
                 workflow_title: str | None = None,
                 request_id: str | None = None) -> dict:
    """Create several issues together, all or nothing (MS-647).

    Each item is a dict of `project`, `title`, and optional `body`, `state`
    (`ready` by default), `rank`, `labels`, `ref` and `after`. `after` lists
    what the item waits on, as MS-646 dependencies: another item of this
    batch by 0-based index or by its `ref`, or an existing issue id. A string
    that names a `ref` of this batch means that item, even if an issue has the
    same id. With `workflow_title` the batch becomes a new workflow at
    positions 1..N in list order, so a batch with no `after` keeps strict
    order; without it the issues are loose. Everything is checked before the
    first row is written. Returns `ids` in list order, `refs` mapping each
    ref to its id, and `workflow_id`.
    """
    items = [_batch_item(i, it) for i, it in enumerate(items)]
    if not items:
        raise BoardError("a batch needs at least one item")
    if workflow_title is not None:
        if not isinstance(workflow_title, str) or not workflow_title.strip():
            raise BoardError("a batch's workflow_title is a non-blank string")
        workflow_title = workflow_title.strip()
    refs = {}
    for i, it in enumerate(items):
        if it["ref"] is not None:
            if it["ref"] in refs:
                raise BoardError(f"item {i}: ref {it['ref']!r} is already item {refs[it['ref']]}")
            refs[it["ref"]] = i
    for i, it in enumerate(items):
        it["inside"], it["outside"] = _batch_after(i, it["after"], refs, len(items))
    _batch_acyclic(items)
    # The title is hashed so a long one still fits the key column on PostgreSQL.
    scope = ("wf-" + hashlib.sha256(workflow_title.encode()).hexdigest()[:16]
             if workflow_title is not None else "loose")
    key = _scoped("create_batch", scope, request_id)

    def result(ids, workflow_id):
        return {"ids": ids, "workflow_id": workflow_id,
                "refs": {it["ref"]: ids[i] for i, it in enumerate(items)
                         if it["ref"] is not None}}

    def replay(s, ev):
        ids = [ev.issue_id] + [
            s.scalar(select(Event.issue_id).where(Event.idempotency_key == _item_key(key, i)))
            for i in range(1, len(items))]
        return result(ids, s.get(Issue, ev.issue_id).workflow_id)

    def body(s):
        for project in {it["project"] for it in items}:
            if s.get(Project, project) is None:
                raise NotFound(f"no project {project!r}")
        for it in items:
            for other in it["outside"]:
                _get(s, other)
        now = store.db_now(s)
        wf = None
        if workflow_title is not None:
            wf = Workflow(title=workflow_title, created_at=now)
            s.add(wf)
            s.flush()
        issues = []
        for i, it in enumerate(items):
            item_key = None if key is None else (key if i == 0 else _item_key(key, i))
            issues.append(_create(s, it["project"], it["title"], actor, actor_kind, it["body"],
                                  it["state"], it["rank"], it["labels"],
                                  wf and wf.id, wf and i + 1, now, item_key))
        for issue, it in zip(issues, items):
            for on in [issues[j].id for j in it["inside"]] + it["outside"]:
                if s.get(Dependency, (issue.id, on)) is not None:
                    continue
                s.add(Dependency(issue_id=issue.id, depends_on_id=on, created_at=now,
                                 created_by=actor))
                _event(s, issue.id, now, actor, actor_kind, "depend", None, None,
                       f"waits on {on}", None)
        s.flush()
        return result([i.id for i in issues], wf and wf.id)

    return _write(engine, key, body, replay,
                  [[{k: it[k] for k in ("project", "title", "body", "state", "rank",
                                        "labels", "ref", "after")} for it in items],
                   workflow_title, actor, actor_kind])


def _item_key(key, i):
    """Item `i`'s key under a batch's `key`. `_scoped` keys start with an
    operation name, so no caller's request id can produce this one."""
    return f"batch-item:{i}:{key}"


def _batch_item(i, it):
    if not isinstance(it, dict):
        raise BoardError(f"item {i}: an item is an object")
    title = it.get("title")
    if not isinstance(title, str) or not title.strip():
        raise BoardError(f"item {i}: every item needs a title")
    title = title.strip()
    if not isinstance(it.get("project"), str) or not it["project"]:
        raise BoardError(f"item {i}: every item needs a project")
    rank = it.get("rank")
    if rank is not None and (not isinstance(rank, int) or isinstance(rank, bool)):
        raise BoardError(f"item {i}: a rank is a whole number")
    labels = it.get("labels") or []
    if not isinstance(labels, list) or not all(isinstance(l, str) and l.strip()
                                               for l in labels):
        raise BoardError(f"item {i}: labels are a list of non-blank strings")
    state = it.get("state") or "ready"
    if state not in CREATE_STATES:
        raise InvalidTransition(f"item {i}: an issue is created {sorted(CREATE_STATES)}, "
                                f"not {state!r}")
    ref = it.get("ref")
    if ref is not None and (not isinstance(ref, str) or not ref):
        raise BoardError(f"item {i}: a ref is a non-empty string")
    after = it.get("after") or []
    if not isinstance(after, list):
        raise BoardError(f"item {i}: after is a list")
    body = it.get("body") or ""
    if not isinstance(body, str):
        raise BoardError(f"item {i}: a body is a string")
    return {"project": it["project"], "title": title, "body": body,
            "state": state, "rank": rank, "labels": sorted({l.strip() for l in labels}),
            "ref": ref, "after": after}


def _batch_after(i, after, refs, n):
    """Split one item's `after` into batch indexes and existing issue ids."""
    inside, outside = [], []
    for a in after:
        if isinstance(a, int) and not isinstance(a, bool):
            if not 0 <= a < n:
                raise BoardError(f"item {i}: after {a} is not an item of this batch")
            j = a
        elif isinstance(a, str) and a in refs:
            j = refs[a]
        elif isinstance(a, str) and a:
            outside.append(a)
            continue
        else:
            raise BoardError(f"item {i}: after {a!r} is neither an index, a ref nor an issue id")
        if j == i:
            raise BoardError(f"item {i} cannot wait on itself")
        inside.append(j)
    return sorted(set(inside)), sorted(set(outside))


def _batch_acyclic(items):
    """Refuse a cycle among the batch's items. Existing issues cannot close
    one: none of them waits on an issue that does not exist yet.

    Items that wait on nothing unsettled settle, round by round; whatever
    never settles is on a cycle or waits on one.
    """
    left = {i: set(it["inside"]) for i, it in enumerate(items)}
    while True:
        free = [i for i, on in left.items() if not on & left.keys()]
        if not free:
            break
        for i in free:
            del left[i]
    if left:
        stuck = ", ".join(str(i) for i in sorted(left))
        raise BoardError(f"items {stuck} wait on each other: a cycle")


def plan(engine, issue_id: str, steps: list[dict], *, actor: str, actor_kind: str,
         token: str | None = None, request_id: str | None = None,
         preempt: bool = False) -> dict:
    """Break an issue into a workflow of steps its caller planned (MS-644).

    Each step is a dict of `title`, optional `body` and optional `project`
    (the origin's by default); each becomes a `ready` issue at positions
    1..N of a new workflow titled after the origin. The origin moves to
    `onhold`, releasing any lease, and returns to `ready` when the last step
    is done, so `next` hands it back to check the result (Chaos, 09-28).
    Both moves are outside TRANSITIONS on purpose. An agent needs its live
    lease on the origin; a human follows the lease rules of `edit`. A second
    plan for one issue raises `Conflict`, and a step cannot be planned.
    """
    steps = [{"title": str(st.get("title") or "").strip(), "body": st.get("body") or "",
              "project": st.get("project") or None} for st in steps]
    if not steps:
        raise BoardError(f"{issue_id}: a plan needs at least one step")
    if not all(st["title"] for st in steps):
        raise BoardError(f"{issue_id}: every step needs a title")
    key = (_scoped("plan", issue_id, request_id)
           or _derive(actor, "plan", issue_id, token, steps))

    def body(s):
        issue = _get(s, issue_id, lock=True)
        now = store.db_now(s)
        if actor_kind == "agent" and issue.lease_holder is None:
            raise LeaseLost(f"{issue.id}: an agent plans an issue only under a lease")
        _guard(s, issue, actor, actor_kind, token, now, preempt, key)
        if issue.workflow_id is not None:
            raise BoardError(f"{issue.id} is step {issue.position} of workflow "
                             f"{issue.workflow_id}; a step cannot be planned")
        if issue.plan is not None:
            raise Conflict(f"{issue.id} already has a plan, workflow {issue.plan.id}")
        if issue.state in ("done", "cancelled"):
            raise InvalidTransition(f"{issue.id} is {issue.state}; it cannot be planned")
        wf = Workflow(title=issue.title, origin_issue_id=issue.id, created_at=now)
        s.add(wf)
        s.flush()
        for position, st in enumerate(steps, 1):
            # The origin's rank, so planning an urgent issue keeps its steps urgent.
            _create(s, st["project"] or issue.project_key, st["title"], actor, actor_kind,
                    st["body"], "ready", issue.rank, (), wf.id, position, now, None)
        old = issue.state
        issue.state = "onhold"
        issue.lease_holder = issue.lease_token = issue.lease_expires_at = None
        issue.version += 1
        issue.updated_at = now
        n = len(steps)
        _event(s, issue.id, now, actor, actor_kind, "transition", old, "onhold",
               f"planned as workflow {wf.id}: {n} step{'' if n == 1 else 's'}",
               None if _has_key(s, key) else key)
        return _view(s, issue)

    return _write(engine, key, body, _replay_view,
                  [steps, actor, actor_kind, token, preempt])


def depend(engine, issue_id: str, on: str, *, actor: str, actor_kind: str,
           request_id: str | None = None) -> dict:
    """Make `issue_id` wait until `on` is done (MS-646).

    `next` skips an issue while anything it depends on is not done. Once any
    step of a workflow declares a dependency, that workflow drops its strict
    order and each step waits only on its own dependencies. Declaring one is
    planning, not work on the issue, so it needs no lease. A self-dependency
    or an edge that would close a cycle is refused; one that already exists
    changes nothing.
    """
    key = _scoped("depend", issue_id, request_id)

    def body(s):
        _lock_dependencies(s)
        issue = _get(s, issue_id, lock=True)
        target = _get(s, on)
        if issue.id == target.id:
            raise BoardError(f"{issue.id} cannot depend on itself")
        if s.get(Dependency, (issue.id, target.id)) is not None:
            return _view(s, issue)
        if _reaches(s, target.id, issue.id):
            raise BoardError(f"{issue.id} -> {target.id} would close a cycle: "
                             f"{target.id} already waits on {issue.id}")
        now = store.db_now(s)
        s.add(Dependency(issue_id=issue.id, depends_on_id=target.id, created_at=now,
                         created_by=actor))
        issue.version += 1
        issue.updated_at = now
        _event(s, issue.id, now, actor, actor_kind, "depend", None, None,
               f"waits on {target.id}", key)
        return _view(s, issue)

    return _write(engine, key, body, _replay_view, [on, actor, actor_kind])


# The advisory lock key every `depend` takes; any constant will do.
_DEPENDENCY_LOCK = 646


def _lock_dependencies(s):
    """Serialise dependency writes, so two cannot each miss half of a cycle.

    Two `depend` calls that would close a cycle lock different issue rows, and
    under PostgreSQL's READ COMMITTED neither sees the other's uncommitted
    edge. This transaction-scoped advisory lock orders them. SQLite needs
    none: BEGIN IMMEDIATE already serialises writers.
    """
    bind = s.get_bind() if hasattr(s, "get_bind") else s
    if bind.dialect.name == "postgresql":
        s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _DEPENDENCY_LOCK})


def undepend(engine, issue_id: str, on: str, *, actor: str, actor_kind: str,
             request_id: str | None = None) -> dict:
    """Remove the dependency of `issue_id` on `on`; a missing one is `NotFound`."""
    key = _scoped("undepend", issue_id, request_id)

    def body(s):
        issue = _get(s, issue_id, lock=True)
        dep = s.get(Dependency, (issue.id, on))
        if dep is None:
            raise NotFound(f"{issue.id} does not depend on {on!r}")
        s.delete(dep)
        now = store.db_now(s)
        issue.version += 1
        issue.updated_at = now
        _event(s, issue.id, now, actor, actor_kind, "undepend", None, None,
               f"no longer waits on {on}", key)
        return _view(s, issue)

    return _write(engine, key, body, _replay_view, [on, actor, actor_kind])


def _reaches(s, start, goal):
    """Whether `goal` is among the issues `start` waits on, directly or not."""
    seen, frontier = {start}, [start]
    while frontier:
        found = s.scalars(select(Dependency.depends_on_id)
                          .where(Dependency.issue_id.in_(frontier))).all()
        if goal in found:
            return True
        frontier = [i for i in set(found) if i not in seen]
        seen.update(frontier)
    return False


def heartbeat(engine, issue_id: str, worker: str, token: str, *,
              ttl: timedelta = DEFAULT_TTL) -> dict:
    """Push the lease expiry forward. Not a state change, so no event.

    Nor does it bump `version`: a web form rendered a minute ago should not
    conflict just because the agent holding the card is still alive.
    """
    with store.session(engine) as s, s.begin():
        issue = _get(s, issue_id, lock=True)
        if issue.lease_holder != worker:
            raise LeaseLost(_lost_message(s, issue))
        _check_token(s, issue, worker, token)
        if issue.lease_expires_at is not None:
            issue.lease_expires_at = store.db_now(s) + ttl
        return {"id": issue.id, "lease_expires_at": _iso(issue.lease_expires_at)}


def edit(engine, issue_id: str, *, actor: str, expected_version: int,
         actor_kind: str = "human", preempt: bool = False, title: str | None = None,
         body: str | None = None, rank: int | None = None,
         labels: list[str] | None = None, state: str | None = None,
         note: str | None = None, request_id: str | None = None,
         attachments: list[dict] = ()) -> dict:
    """A change from the web UI (design section 6).

    Under someone else's lease this raises `LeaseHeld` unless `preempt`. On
    preempt the lease moves to the actor with no expiry and the issue stays
    processing until an explicit release (Chaos's decision 1, section 11).
    `expected_version` is the version the form was rendered from. Saving a
    form that changes nothing writes nothing and returns the issue. Only a
    human or the system edits; an agent works under its lease through
    `transition`, `annotate` and `link`.
    """
    if actor_kind == "agent":
        raise BoardError(f"{issue_id}: an agent cannot edit; it changes an issue under "
                         "its lease through transition, annotate or link")

    request_id = _scoped("edit", issue_id, request_id)

    def run(s):
        issue = _get(s, issue_id, lock=True)
        now = store.db_now(s)
        # Compare before a preempt bumps the version; both roll back together.
        if issue.version != expected_version:
            raise Conflict(f"{issue.id} is at version {issue.version}, "
                           f"the form showed {expected_version}")
        _guard(s, issue, actor, actor_kind, None, now, preempt, request_id)
        changed = []
        for field, value in (("title", title), ("body", body), ("rank", rank)):
            if value is not None and getattr(issue, field) != value:
                setattr(issue, field, value)
                changed.append(field)
        if labels is not None and sorted(set(labels)) != [l.name for l in issue.labels]:
            issue.labels = [Label(name=n) for n in sorted(set(labels))]
            changed.append("labels")
        # With a state change the files are that transition's reason (MS-661).
        if attachments and state is None:
            _attach(s, issue, None, attachments, actor, now)
            changed.append("attachments")
        if changed:
            issue.version += 1
            issue.updated_at = now
            key = None if _has_key(s, request_id) else request_id
            _event(s, issue.id, now, actor, actor_kind, "edit", None, None,
                   "changed " + ", ".join(changed), key)
        if state is not None:
            ev = _apply_state(s, issue, state, note, actor, actor_kind, now, request_id,
                              key_used=_has_key(s, request_id))
            _attach(s, issue, ev.id, attachments, actor, now)
        return _view(s, issue)

    return _write(engine, request_id, run, _replay_view,
                  [expected_version, preempt, title, body, rank,
                   sorted(set(labels)) if labels is not None else None, state, note,
                   actor, actor_kind, *_digests(attachments)])


# --- internals ------------------------------------------------------------------


def _candidates(now, project):
    """Workable issues in rank order (design section 4).

    `FOR UPDATE SKIP LOCKED` is rendered on PostgreSQL only; SQLAlchemy drops
    it on SQLite, where BEGIN IMMEDIATE already serialises the callers.
    """
    earlier = aliased(Issue)
    blocker = aliased(Issue)
    sibling = aliased(Issue)
    q = (
        select(Issue)
        .where(
            or_(
                Issue.state == "ready",
                and_(Issue.state == "processing",
                     Issue.lease_expires_at.is_not(None),
                     Issue.lease_expires_at < now),
            ),
            ~exists().where(Artifact.issue_id == Issue.id, Artifact.closes.is_(True)),
            ~exists().where(Dependency.issue_id == Issue.id,
                            blocker.id == Dependency.depends_on_id,
                            blocker.state != "done"),
            or_(
                Issue.workflow_id.is_(None),
                # A step of a workflow that declares dependencies waits only
                # on its own (MS-646); otherwise the workflow is strict.
                exists().where(sibling.workflow_id == Issue.workflow_id,
                               Dependency.issue_id == sibling.id),
                ~exists().where(earlier.workflow_id == Issue.workflow_id,
                                earlier.position < Issue.position,
                                earlier.state != "done"),
            ),
        )
        .order_by(Issue.rank, Issue.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=Issue)
    )
    if project is not None:
        q = q.where(Issue.project_key == project)
    return q


def _create(s, project, title, actor, actor_kind, body, state, rank, labels,
            workflow_id, position, now, key):
    number = s.scalar(
        update(Project).where(Project.key == project)
        .values(next_number=Project.next_number + 1)
        .returning(Project.next_number)
    )
    if number is None:
        raise NotFound(f"no project {project!r}")
    if rank is None:
        rank = (s.scalar(select(func.max(Issue.rank))) or 0) + 10
    issue = Issue(id=f"{project}-{number - 1}", project_key=project, title=title,
                  body=body, state=state, rank=rank, workflow_id=workflow_id,
                  position=position, created_at=now, updated_at=now,
                  labels=[Label(name=n) for n in sorted(set(labels))])
    s.add(issue)
    s.flush()
    _event(s, issue.id, now, actor, actor_kind, "create", None, state, None, key)
    return issue


def _apply_state(s, issue, state, note, actor, actor_kind, now, key, key_used):
    if state not in TRANSITIONS:
        raise InvalidTransition(f"unknown state {state!r}")
    if state in NOTE_REQUIRED and not note:
        raise NoteRequired(f"a transition into {state} needs a note")
    old = issue.state
    if state != old:
        if state not in TRANSITIONS[old]:
            raise InvalidTransition(f"{issue.id}: {old} -> {state} is not allowed")
        if state == "done" and actor_kind == "agent" and issue.workflow_id is None:
            raise InvalidTransition(f"{issue.id}: only a human closes a card that is not a "
                                    "workflow step; move it to need-input with a review note")
        issue.state = state
        if old == "processing":
            issue.lease_holder = issue.lease_token = issue.lease_expires_at = None
        issue.version += 1
        issue.updated_at = now
    ev = _event(s, issue.id, now, actor, actor_kind, "transition", old, state, note,
                None if key_used else key)
    if state == "done" and old != "done":
        _resume_origin(s, issue, now)
    return ev


def _resume_origin(s, step, now):
    """The last step of a plan is done: the held origin goes back to `ready`.

    An origin a human has moved off `onhold` meanwhile is left where it is.
    """
    wf = step.workflow
    if wf is None or wf.origin_issue_id is None:
        return
    # Lock the origin before reading the steps. Parallel steps (MS-646) can
    # finish together, and under READ COMMITTED each would see the other still
    # open; the lock orders them and the fresh read sees the first one's commit.
    origin = _get(s, wf.origin_issue_id, lock=True)
    states = list(s.scalars(select(Issue.state).where(Issue.workflow_id == wf.id)))
    if not states or any(st != "done" for st in states):
        return
    if origin.state != "onhold":
        return
    origin.state = "ready"
    origin.version += 1
    origin.updated_at = now
    _event(s, origin.id, now, "board", "system", "transition", "onhold", "ready",
           f"every step of workflow {wf.id} is done; check the result and move this to "
           "need-input for a human to close", None)


def _guard(s, issue, actor, actor_kind, token, now, preempt, key):
    """Lease rules for a change (not a note) to `issue`."""
    if actor_kind == "agent":
        _check_token(s, issue, actor, token)
        return
    if issue.lease_holder is None or issue.lease_holder == actor:
        return
    if not preempt:
        raise LeaseHeld(issue.lease_holder, issue.lease_expires_at)
    prev = issue.lease_holder
    issue.lease_holder = actor
    issue.lease_token = secrets.token_hex(16)
    issue.lease_expires_at = None
    issue.version += 1
    issue.updated_at = now
    _event(s, issue.id, now, actor, actor_kind, "preempt", issue.state, issue.state,
           f"lease taken over from {prev}", None if _has_key(s, key) else key)


def _check_token(s, issue, actor, token):
    if issue.lease_holder is None and token is None:
        return
    if token is None or token != issue.lease_token or issue.lease_holder != actor:
        raise LeaseLost(_lost_message(s, issue))


def _lost_message(s, issue):
    if issue.lease_holder is None:
        return f"{issue.id}: lease lost, the issue is no longer held ({issue.state})"
    last = s.scalar(
        select(Event)
        .where(Event.issue_id == issue.id, Event.kind.in_(("preempt", "reclaim", "claim")))
        .order_by(Event.id.desc())
    )
    how = {"preempt": "preempted", "reclaim": "reclaimed", "claim": "claimed"}
    detail = f" at {_iso(last.at)}" if last is not None else ""
    verb = how.get(last.kind, "held") if last is not None else "held"
    return f"{issue.id}: lease lost, {verb} by {issue.lease_holder}{detail}"


def _event(s, issue_id, at, actor, actor_kind, kind, from_state, to_state, note, key):
    if actor_kind not in ACTOR_KINDS:
        raise BoardError(f"actor kind {actor_kind!r} is not one of {sorted(ACTOR_KINDS)}")
    ev = Event(issue_id=issue_id, at=at, actor=actor, actor_kind=actor_kind, kind=kind,
               from_state=from_state, to_state=to_state, note=note, idempotency_key=key)
    s.add(ev)
    s.flush()
    return ev


def _attach(s, issue, event_id, files, actor, now):
    """Record files already written to disk: dicts of filename, content_type, size, sha256.

    The digest names the file on disk, so it must be a SHA-256 and nothing else.
    """
    for f in files:
        if not re.fullmatch(r"[0-9a-f]{64}", str(f["sha256"])):
            raise BoardError(f"{f['filename']!r} has no valid SHA-256 digest")
    for f in files:
        s.add(Attachment(issue_id=issue.id, event_id=event_id, filename=f["filename"],
                         content_type=f["content_type"], size=f["size"], sha256=f["sha256"],
                         added_at=now, added_by=actor))
    if files:
        s.flush()


def _digests(files):
    """What identifies a call's files for a request hash: name and content.

    A list to splat onto the hashed arguments. It is empty without files, so a
    fileless call hashes as it did before attachments existed.
    """
    return [[[f["filename"], f["sha256"]] for f in files]] if files else []


def _attachment_view(a):
    return {"id": a.id, "filename": a.filename, "content_type": a.content_type,
            "size": a.size, "event_id": a.event_id, "added_by": a.added_by,
            "added_at": _iso(a.added_at)}


def _has_key(s, key):
    return key is not None and s.scalar(
        select(Event.id).where(Event.idempotency_key == key)) is not None


def _scoped(op, target, request_id):
    """Scope a caller's request id to one operation on one target.

    A caller that reuses an id for a different call is not retrying, so
    that call must run rather than replay the first one's result.
    """
    if request_id is None:
        return None
    return f"{op}:{target}:{request_id}"


def _derive(actor, op, issue_id, token, *args):
    """Design section 8: an exact retry under one lease collapses to one event.

    Only under a lease. Without a token there is no lease to scope the key
    to, so two identical human notes stay two events.
    """
    if token is None:
        return None
    raw = json.dumps([actor, op, issue_id, token, *args], default=str)
    return "derived:" + hashlib.sha256(raw.encode()).hexdigest()


def _write(engine, key, body, replay, args):
    """Run `body` in one transaction, or replay the call that already used `key`.

    `args` are the call's arguments. Their hash is stored beside the key, so
    a request id that returns with other arguments names a different call
    and raises `Conflict` rather than replaying (design section 8). A replay
    returns the issue as it is now, not as the first call left it.
    """
    digest = hashlib.sha256(json.dumps(args, default=str).encode()).hexdigest()
    try:
        with store.session(engine) as s, s.begin():
            if key is not None:
                ev = s.scalar(select(Event).where(Event.idempotency_key == key))
                if ev is not None:
                    return _replay(s, ev, digest, replay)
            result = body(s)
            if key is not None:
                s.execute(update(Event).where(Event.idempotency_key == key)
                          .values(request_hash=digest)
                          .execution_options(synchronize_session=False))
            return result
    except (IntegrityError, BoardError):
        # A concurrent retry with the same key committed first. Either its
        # key insert collided, or on PostgreSQL it waited on the row lock and
        # then found the issue already moved on by the call it repeats.
        if key is None:
            raise
        with store.session(engine) as s:
            ev = s.scalar(select(Event).where(Event.idempotency_key == key))
            if ev is None:
                raise
            return _replay(s, ev, digest, replay)


def _replay(s, ev, digest, replay):
    if ev.request_hash is not None and ev.request_hash != digest:
        raise Conflict(f"request id {ev.idempotency_key!r} already named another call")
    return replay(s, ev)


def _replay_view(s, ev):
    return _view(s, s.get(Issue, ev.issue_id))


def _get(s, issue_id, lock=False):
    # A write path locks the row before checking a token or a version.
    # Without it, PostgreSQL's READ COMMITTED lets a stale check pass while
    # `next` reclaims or a human preempts, and the later UPDATE wins.
    issue = s.get(Issue, issue_id, with_for_update=lock or None)
    if issue is None:
        raise NotFound(f"no issue {issue_id!r}")
    return issue


def _iso(dt):
    return dt.isoformat() if dt is not None else None


def _view(s, issue):
    s.flush()
    s.refresh(issue)
    return {
        "id": issue.id,
        "project": issue.project_key,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "rank": issue.rank,
        "assignee": issue.assignee,
        "labels": [l.name for l in issue.labels],
        "lease_holder": issue.lease_holder,
        "lease_expires_at": _iso(issue.lease_expires_at),
        "version": issue.version,
        "created_at": _iso(issue.created_at),
        "updated_at": _iso(issue.updated_at),
        "closed_by_artifact": any(a.closes for a in issue.artifacts),
        "artifacts": [
            {"kind": a.kind, "ref": a.ref, "closes": a.closes, "added_by": a.added_by,
             "added_at": _iso(a.added_at)}
            for a in issue.artifacts
        ],
        "attachments": [_attachment_view(a) for a in issue.attachments],
        "events": [
            {"id": e.id, "at": _iso(e.at), "actor": e.actor, "actor_kind": e.actor_kind,
             "kind": e.kind, "from_state": e.from_state, "to_state": e.to_state,
             "note": e.note,
             "attachments": [_attachment_view(a) for a in issue.attachments
                             if a.event_id == e.id]}
            for e in issue.events
        ],
        "workflow": (_workflow_view(s, issue.workflow) | {"position": issue.position}
                     if issue.workflow is not None else None),
        "plan": _workflow_view(s, issue.plan) if issue.plan is not None else None,
        "depends_on": _linked(s, Dependency.depends_on_id, Dependency.issue_id, issue.id),
        "blocks": _linked(s, Dependency.issue_id, Dependency.depends_on_id, issue.id),
    }


def _linked(s, far, near, issue_id):
    """The issues at the `far` end of this issue's dependency rows, in id order."""
    rows = s.execute(select(Issue.id, Issue.title, Issue.state)
                     .join(Dependency, Issue.id == far).where(near == issue_id)
                     .order_by(Issue.id))
    return [{"id": i, "title": t, "state": st} for i, t, st in rows]


def _workflow_state(steps):
    """PRD section 5: done when every step is done, need-input when any is.

    In a strict workflow every step is done once the last one is; with
    declared dependencies (MS-646) the last position can finish first.

    A workflow nobody has started sits in `ready` when its first step is
    ready, because that is the step `next` would hand out.
    """
    if _all_done(steps):
        return "done"
    if any(st.state == "need-input" for st in steps):
        return "need-input"
    if any(st.state in ("processing", "done") for st in steps):
        return "processing"
    if steps and steps[0].state == "ready":
        return "ready"
    return "backlog"


def _all_done(steps):
    return bool(steps) and all(st.state == "done" for st in steps)


def _workflow_view(s, wf):
    s.refresh(wf)
    # Each step's dependencies, inside the workflow or not, for the chart (MS-646).
    deps = {st.id: [] for st in wf.steps}
    for issue_id, on in s.execute(
            select(Dependency.issue_id, Dependency.depends_on_id)
            .where(Dependency.issue_id.in_(list(deps)))
            .order_by(Dependency.depends_on_id)):
        deps[issue_id].append(on)
    return {
        "id": wf.id,
        "title": wf.title,
        "origin_issue_id": wf.origin_issue_id,
        "state": _workflow_state(wf.steps),
        "steps": [{"id": st.id, "position": st.position, "title": st.title,
                   "state": st.state, "depends_on": deps[st.id]} for st in wf.steps],
    }
