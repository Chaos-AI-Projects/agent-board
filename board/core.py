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
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadDateError, croniter

from sqlalchemy import and_, case, exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from board import store
from board.store import (Artifact, Attachment, CardSeen, Dependency, Event, Issue, Label, Project, Template,
                         TemplateStep, Workflow)

DEFAULT_TTL = timedelta(minutes=90)

# AB-52, brain plans/agent-board-lanes.md section 2. A card's stage is its
# bottom lane, stored in `issue.state`; its queue status is ready, processing
# or on-hold, or None when it is not queued. Only a work stage is queued.
STAGES = ("backlog", "planning", "implement", "done", "cancelled")
WORK_STAGES = {"planning", "implement"}
QUEUES = ("ready", "processing", "on-hold")
STAGE_MOVES: dict[str, set[str]] = {
    "backlog": {"planning", "implement", "done", "cancelled"},
    "planning": {"implement", "done", "backlog", "cancelled"},
    "implement": {"planning", "done", "backlog", "cancelled"},
    "done": {"implement"},
    "cancelled": set(),
}
# Within a work stage. `processing` is entered only by `next`.
QUEUE_MOVES: dict[str | None, set[str | None]] = {
    None: {"ready", "on-hold"},
    "ready": {"on-hold"},
    "processing": {None, "ready", "on-hold"},
    "on-hold": {None},
}
CREATE_STAGES = {"backlog", "planning", "implement"}
# AB-53: the web board's hover text for each stage lane and the queue strip.
STAGE_HINTS = {
    "backlog": "Ideas nobody has asked for yet. Drag one to planning or implement to "
               "queue it.",
    "planning": "Cards to plan. Waiting here, out of the queue, means the plan is "
                "written and waits for you: drag to implement to approve it.",
    "implement": "Cards to build. Waiting here, out of the queue, means the agent "
                 "has a question or finished work for you. Queue sends it back.",
    "done": "Finished. An agent closes with a note such as a PR; you may close "
            "without one. Dragging back to implement reopens it with a reason.",
    "cancelled": "Dropped. Final.",
}
QUEUE_HINT = ("Queued planning and implement cards: ready waits for board next, "
              "processing is held by an agent, on-hold is frozen. Not draggable; "
              "change a queued card from its page.")
READY = ("implement", "ready")
# AB-54, plan section 4: the event a schedule change writes.
SCHEDULE_EVENT = "schedule"
# A ready scheduled card joins the top strip this long before it is due.
SCHEDULE_LEAD = timedelta(minutes=5)
# AB-66: the closest two fire times of one schedule may fall, in minutes.
SCHEDULE_MIN_GAP = 5
# Every weekday falls on every date, leap day included, within 28 years.
CALENDAR_CYCLE_YEARS = 28
# croniter's R item, bare or as R(a-b) or R/n, rolls a new value on every read.
RANDOM_ITEM = re.compile(r"r(\(|/|$)", re.IGNORECASE)
SCHEDULED_HINT = ("Ready cron cards not due for more than five minutes. Each joins the "
                  "queue strip as its due time nears. Not draggable.")
TZ_ENV = "BOARD_TIMEZONE"
# The old state names, which only the backlog importer and the web's check for a
# stale lanes preference still read (AB-56). Migration 0010 holds the same table.
LEGACY_SPLIT: dict[str, tuple[str, str | None]] = {
    "backlog": ("backlog", None),
    "ready": ("implement", "ready"),
    "processing": ("implement", "processing"),
    "need-input": ("implement", None),
    "onhold": ("implement", "on-hold"),
    "done": ("done", None),
    "cancelled": ("cancelled", None),
}
ARTIFACT_KINDS = {"commit", "pr", "path", "url"}
ARTIFACT_NAME_MAX = 200
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


RECENT_DAYS = 14


def recent_projects(engine, actor: str, days: int = RECENT_DAYS) -> list[str]:
    """The keys of projects where `actor` wrote any event in the last `days` days.

    Creating an issue writes its create event, so this covers created issues
    as well as notes, moves, edits and links (AB-12).
    """
    with store.session(engine) as s:
        since = store.db_now(s) - timedelta(days=days)
        return list(s.scalars(
            select(Issue.project_key).distinct().join(Event, Event.issue_id == Issue.id)
            .where(Event.actor == actor, Event.at >= since).order_by(Issue.project_key)))


DONE_HIDE_DAYS = 14


def done_times(engine) -> dict[str, str]:
    """When each done issue finished, by id: its latest move into done (AB-13).

    A create event counts, since the backfill wrote cards straight into done.
    A repeated move to done changes nothing, so it does not restart the clock.
    An issue with no such event falls back to its `updated_at`.
    """
    with store.session(engine) as s:
        arrived = (select(Event.issue_id, func.max(Event.at).label("at"))
                   .where(Event.to_state == "done",
                          or_(Event.from_state.is_(None), Event.from_state != "done"))
                   .group_by(Event.issue_id).subquery())
        rows = s.execute(select(Issue.id, func.coalesce(arrived.c.at, Issue.updated_at))
                         .outerjoin(arrived, arrived.c.issue_id == Issue.id)
                         .where(Issue.state == "done").order_by(Issue.id))
        return {i: _iso(at) for i, at in rows}


def _finishes():
    """Events that end an agent's run: its card leaves processing (AB-67)."""
    return and_(Event.actor_kind == "agent", Event.from_queue == "processing",
                or_(Event.to_queue.is_(None), Event.to_queue != "processing"))


def unseen(engine, actor: str) -> set[str]:
    """The ids of finished cards `actor` has not looked at since (AB-67).

    A card is theirs when they created it, an agent created it for them, or
    they wrote any event on it as a human. It is unseen when an agent's run
    on it ended after both their last opening of it and their own last
    event on it, and no agent is working it again now.
    """
    with store.session(engine) as s:
        mine = _by_person(actor)
        theirs = _involves(actor)
        finished = (select(func.max(Event.id)).where(Event.issue_id == Issue.id, _finishes())
                    .correlate(Issue).scalar_subquery())
        acted = (select(func.max(Event.id)).where(Event.issue_id == Issue.id, mine)
                 .correlate(Issue).scalar_subquery())
        opened = (select(CardSeen.event_id)
                  .where(CardSeen.issue_id == Issue.id, CardSeen.actor == actor)
                  .correlate(Issue).scalar_subquery())
        rows = s.scalars(select(Issue.id).where(
            theirs, or_(Issue.queue.is_(None), Issue.queue != "processing"),
            finished > func.coalesce(opened, 0), finished > func.coalesce(acted, 0)))
        return set(rows)


def _by_person(actor: str):
    """An event `actor` wrote as a human."""
    return (Event.actor == actor) & (Event.actor_kind == "human")


def _involves(actor: str):
    """Whether the issue is `actor`'s (AB-67, AB-70): they created it, an agent
    created it for them, or they wrote any event on it as a human."""
    return exists().where(Event.issue_id == Issue.id,
                          or_(_by_person(actor), (Event.kind == "create")
                              & (Event.on_behalf_of == actor)))


def mark_seen(engine, actor: str, issue_id: str) -> None:
    """Record that `actor` has the card in front of them, as of its newest event."""
    for _ in range(2):
        try:
            with store.session(engine) as s, s.begin():
                _get(s, issue_id)
                newest = s.scalar(select(func.max(Event.id)).where(Event.issue_id == issue_id))
                row = s.get(CardSeen, (actor, issue_id))
                if row is None:
                    s.add(CardSeen(actor=actor, issue_id=issue_id, event_id=newest or 0))
                elif row.event_id != newest:
                    row.event_id = newest or 0
            return
        except IntegrityError:
            # Two pages opened the card at once and the other inserted first;
            # the second pass updates its row.
            continue


def lane_meta(engine) -> dict[str, dict]:
    """Each issue's card meta line, by id (AB-35).

    `entered_at` is its latest move into the state it is in now, falling back
    to its `updated_at`, as `done_times` does for done. `pr` is the ref of its
    newest PR artifact, or None. `urls` holds every url artifact's ref and
    name in link order (AB-40, AB-43): unlike a PR, a later link does not
    replace an earlier one. `last_actor_kind` is the actor kind of its newest
    event: the Queue button shows only when that was a human, so an agent or
    system event hides it (AB-58).
    """
    with store.session(engine) as s:
        arrived = (select(Event.issue_id, Event.to_state, Event.to_queue,
                          func.max(Event.at).label("at"))
                   .where(Event.to_state.is_not(None),
                          or_(Event.from_state.is_(None), Event.from_state != Event.to_state,
                              Event.from_queue.is_distinct_from(Event.to_queue)))
                   .group_by(Event.issue_id, Event.to_state, Event.to_queue).subquery())
        rows = s.execute(select(Issue.id, func.coalesce(arrived.c.at, Issue.updated_at))
                         .outerjoin(arrived, (arrived.c.issue_id == Issue.id)
                                    & (arrived.c.to_state == Issue.state)
                                    & arrived.c.to_queue.is_not_distinct_from(Issue.queue)))
        meta = {i: {"entered_at": _iso(at), "pr": None, "urls": [], "last_actor_kind": None}
                for i, at in rows}
        newest = select(func.max(Event.id)).group_by(Event.issue_id)
        for issue_id, actor_kind in s.execute(select(Event.issue_id, Event.actor_kind)
                                              .where(Event.id.in_(newest))):
            meta[issue_id]["last_actor_kind"] = actor_kind
        for issue_id, kind, ref, name in s.execute(
                select(Artifact.issue_id, Artifact.kind, Artifact.ref, Artifact.name)
                .where(Artifact.kind.in_(("pr", "url"))).order_by(Artifact.id)):
            if kind == "pr":
                meta[issue_id]["pr"] = ref
            else:
                meta[issue_id]["urls"].append({"ref": ref, "name": name})
        return meta


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
            prev_holder, prev_queue = issue.lease_holder, issue.queue
            token = secrets.token_hex(16)
            # Fires missed while the card waited collapse into this one run.
            due = ({"next_run_at": next_due(issue.schedule, issue.schedule_tz, now)}
                   if issue.schedule and not issue.schedule_paused else {})
            won = s.execute(
                update(Issue)
                .where(Issue.id == issue.id, Issue.version == issue.version)
                .values(queue="processing", lease_holder=worker, lease_token=token,
                        lease_expires_at=now + ttl, version=Issue.version + 1,
                        updated_at=now, **due)
                .execution_options(synchronize_session=False)
            ).rowcount
            if won != 1:
                s.expire(issue)
                continue
            if prev_holder is not None:
                kind, note = "reclaim", f"lease expired; preempted holder {prev_holder}"
            else:
                kind, note = "claim", None
            _event(s, issue.id, now, worker, "agent", kind, issue.state, issue.state,
                   note, key, prev_queue, "processing")
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
             label: str | None = None, assignee: str | None = None,
             creator: str | None = None, involved: str | None = None) -> dict:
    """Everything the web board renders, read in one session.

    `now` is the database clock, so the board judges a lease expired by the
    same clock `next` uses to reclaim it. Issues carry no events or token.
    The filters are `search`'s; `labels`, `assignees`, `creators` and
    `people` are every value in use, for the filter dropdowns, whatever the
    filters matched. `people` is everyone `involved` can match (AB-70).
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
            "creators": sorted(s.scalars(select(_creator_col()).distinct()
                                         .where(Event.kind == "create"))),
            "templates": [{"name": t.name, "title": t.title}
                          for t in s.scalars(select(Template).order_by(Template.name))],
            "people": sorted(
                set(s.scalars(select(Event.actor).distinct()
                              .where(Event.actor_kind == "human")))
                | set(s.scalars(select(Event.on_behalf_of).distinct()
                                .where(Event.on_behalf_of.is_not(None))))),
            "issues": _search(s, q, project, label, assignee, creator, involved),
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
        open_steps = [st["id"] for st in view["steps"] if st["stage"] != "done"]
        return view | {
            "now": _iso(store.db_now(s)),
            "current": open_steps[0] if open_steps else None,
            "steps": [_row(i) for i in wf.steps],
        }


def search(engine, *, q: str | None = None, project: str | None = None,
           label: str | None = None, assignee: str | None = None,
           creator: str | None = None, involved: str | None = None) -> list[dict]:
    """Issues matching every filter given, the newest updated first (AB-72).

    `q` is a case-insensitive substring of the id, title, body or any label,
    matched with plain `lower() LIKE` so the schema needs no full-text
    extension on either engine. `%` and `_` in it are literal characters.
    `project`, `label` and `assignee` match exactly. Nothing writes an
    issue's `assignee` yet, so `assignee` also matches the lease holder: the
    agent or human holding a card is the one working it. `creator` is the
    actor of the issue's create event (AB-27), or the person an agent named
    on it (AB-63). `involved` is a person who created the issue, had it
    created for them, or wrote any event on it as a human (AB-70). An empty
    filter is no filter. Cards updated in the same instant keep board order.
    """
    with store.session(engine) as s:
        return _search(s, q, project, label, assignee, creator, involved, newest_first=True)


def _creator_col():
    """A create event's creator: the person named for it, else its actor (AB-63)."""
    return func.coalesce(Event.on_behalf_of, Event.actor)


def _search(s, q, project, label, assignee, creator=None, involved=None, newest_first=False):
    stmt = select(Issue)
    if newest_first:
        stmt = stmt.order_by(Issue.updated_at.desc())
    stmt = stmt.order_by(Issue.rank, Issue.id)
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
    if creator:
        stmt = stmt.where(exists().where(Event.issue_id == Issue.id, Event.kind == "create",
                                         _creator_col() == creator))
    if involved:
        stmt = stmt.where(_involves(involved))
    return [_row(i) for i in s.scalars(stmt)]


STATS_DAYS = 14
STATS_DAYS_MAX = 3650


SERVICE_ACCOUNT_SUFFIX = ".gserviceaccount.com"


def counted_kind(actor: str, kind: str, agents=frozenset()) -> str:
    """The kind /stats counts an event under (AB-48).

    Before AB-41, a call made with a human's token was stamped `agent`. An
    email that is not a service account's and is not in `agents` (the
    emails BOARD_SA_MAP maps service accounts to) is a human's, so its
    agent events count as human. A bare name such as `overlord` stays agent.
    """
    who = actor.lower()
    if (kind == "agent" and "@" in who and not who.endswith(SERVICE_ACCOUNT_SUFFIX)
            and who not in agents):
        return "human"
    return kind


def stats(engine, *, days: int = STATS_DAYS, limit: int = 50, agents=frozenset()) -> dict:
    """What each actor did in the last `days` days, read from the event log (AB-29).

    Per actor and actor kind: `created` counts the cards each one created,
    crediting a card an agent entered for a person to that person (AB-63),
    who counts as human. `moved` counts
    events that changed an issue's state (a claim or a transition), `done`
    the moves into done, `noted` the annotations, and `events` every event of
    any kind. A reclaim, a preempt, or a transition to the state the issue is
    already in records an event but is not a move. `actors` is busiest first.
    `activity` is the newest `limit` events in the window, newest first by
    event id, because imported events share one timestamp. Kinds are as
    `counted_kind` reads them, with `agents` passed through.
    """
    if not 1 <= days <= STATS_DAYS_MAX:
        raise BoardError(f"the window is 1 to {STATS_DAYS_MAX} days, not {days}")
    moved = and_(Event.from_state.is_not(None), Event.to_state.is_not(None),
                 or_(Event.from_state != Event.to_state,
                     Event.from_queue.is_distinct_from(Event.to_queue)))

    def count(cond):
        return func.count(case((cond, 1)))

    with store.session(engine) as s:
        now = store.db_now(s)
        since = now - timedelta(days=days)
        rows = s.execute(
            select(Event.actor, Event.actor_kind,
                   count(and_(Event.kind == "create", Event.on_behalf_of.is_(None))),
                   count(moved),
                   count(and_(moved, Event.to_state == "done")),
                   count(Event.kind == "annotate"), func.count())
            .where(Event.at >= since).group_by(Event.actor, Event.actor_kind)).all()
        entered = s.execute(
            select(Event.on_behalf_of, func.count())
            .where(Event.at >= since, Event.kind == "create", Event.on_behalf_of.is_not(None))
            .group_by(Event.on_behalf_of)).all()
        activity = s.execute(
            select(Event, Issue.title).join(Issue, Event.issue_id == Issue.id)
            .where(Event.at >= since).order_by(Event.id.desc()).limit(limit)).all()
        actors: dict[tuple[str, str], dict] = {}
        for a, k, *counts in rows:
            kind = counted_kind(a, k, agents)
            row = actors.setdefault((a, kind), {"actor": a, "actor_kind": kind, "created": 0,
                                                "moved": 0, "done": 0, "noted": 0,
                                                "events": 0})
            for name, n in zip(("created", "moved", "done", "noted", "events"), counts):
                row[name] += n
        for person, n in entered:
            actors.setdefault((person, "human"), {"actor": person, "actor_kind": "human",
                                                  "created": 0, "moved": 0, "done": 0,
                                                  "noted": 0, "events": 0})["created"] += n
        return {
            "now": _iso(now), "since": _iso(since), "days": days,
            "actors": sorted(actors.values(),
                             key=lambda r: (-r["events"], r["actor"], r["actor_kind"])),
            "activity": [{"issue_id": ev.issue_id, "title": title, "at": _iso(ev.at),
                          "actor": ev.actor,
                          "actor_kind": counted_kind(ev.actor, ev.actor_kind, agents),
                          "kind": ev.kind,
                          "from_stage": ev.from_state, "to_stage": ev.to_state,
                          "from_queue": ev.from_queue, "to_queue": ev.to_queue,
                          "note": ev.note}
                         for ev, title in activity],
        }


def _row(i):
    """An issue as a board card: no body, events or artifacts."""
    return {
        "id": i.id, "project": i.project_key, "title": i.title,
        "stage": i.state, "queue": i.queue,
        "rank": i.rank, "labels": [l.name for l in i.labels], "assignee": i.assignee,
        "lease_holder": i.lease_holder, "lease_expires_at": _iso(i.lease_expires_at),
        "version": i.version, "workflow_id": i.workflow_id, "position": i.position,
        "updated_at": _iso(i.updated_at), "schedule": i.schedule,
        "schedule_tz": i.schedule_tz, "next_run_at": _iso(i.next_run_at),
        "schedule_paused": i.schedule_paused,
    }


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def move(engine, issue_id: str, stage: str, *, actor: str, actor_kind: str,
         note: str | None = None, token: str | None = None,
         request_id: str | None = None, preempt: bool = False,
         expected_version: int | None = None, attachments: list[dict] = ()) -> dict:
    """Set an issue's stage (plan section 2, AB-52).

    Entering planning or implement queues the card as ready; any other stage
    takes it out of the queue. Moving to the stage it is in changes nothing.
    Reopening a done card needs a reason and no lease.
    `expected_version`, when given, is the version a web page showed; any
    other raises `Conflict` (AB-53). `attachments` hang off the transition
    event, as they do off an `annotate` note (AB-56).
    """
    if stage not in STAGES:
        raise InvalidTransition(f"unknown stage {stage!r}")

    def place(issue):
        if stage == issue.state:
            return stage, issue.queue
        return stage, ("ready" if stage in WORK_STAGES else None)

    return _place_op(engine, "move", issue_id, place, [stage], actor, actor_kind, note,
                     token, request_id, preempt, expected_version, attachments)


def queue(engine, issue_id: str, status: str | None, *, actor: str, actor_kind: str,
          note: str | None = None, token: str | None = None,
          request_id: str | None = None, preempt: bool = False,
          expected_version: int | None = None, attachments: list[dict] = ()) -> dict:
    """Set an issue's queue status: ready, on-hold, or None (plan section 2, AB-52).

    Only `next` sets processing. Releasing a claimed card to None needs a
    note; the card then waits in its stage's lane, as need-input did.
    `expected_version` and `attachments` work as in `move`.
    """
    status = status or None
    if status is not None and status not in QUEUES:
        raise InvalidTransition(f"unknown queue status {status!r}")
    if status == "processing":
        raise InvalidTransition(f"{issue_id}: only board next sets processing")
    return _place_op(engine, "queue", issue_id, lambda issue: (issue.state, status),
                     [status], actor, actor_kind, note, token, request_id, preempt,
                     expected_version, attachments)


def _place_op(engine, op, issue_id, place, args, actor, actor_kind, note, token,
              request_id, preempt, expected_version=None, attachments=()):
    key = (_scoped(op, issue_id, request_id)
           or _derive(actor, op, issue_id, token, *args, note, *_digests(attachments)))

    def body(s):
        issue = _get(s, issue_id, lock=True)
        if expected_version is not None and issue.version != expected_version:
            raise Conflict(f"{issue.id} is at version {issue.version}, "
                           f"the page showed {expected_version}")
        now = store.db_now(s)
        stage, queue = place(issue)
        if actor_kind == "agent" and issue.lease_holder is None:
            reopens = issue.state == "done" and stage != "done"
            if not (_left_by(s, issue, actor, stage) or reopens):
                raise LeaseLost(f"{issue.id}: an agent moves an issue only under a lease")
        else:
            _guard(s, issue, actor, actor_kind, token, now, preempt, None)
        _judge(issue, stage, queue, note, actor_kind)
        ev = _set_place(s, issue, stage, queue, note, actor, actor_kind, now, key,
                        key_used=False)
        _attach(s, issue, ev.id, attachments, actor, now)
        return _view(s, issue)

    return _write(engine, key, body, _replay_view,
                  [*args, note, actor, actor_kind, token, preempt, expected_version,
                   *_digests(attachments)])


def annotate(engine, issue_id: str, note: str, *, actor: str, actor_kind: str,
             token: str | None = None, request_id: str | None = None,
             attachments: list[dict] = ()) -> dict:
    """Append a note. A human note needs no lease; it cannot overwrite anything.

    `attachments` are files already stored on disk, as `_attach` describes;
    they hang off this note's event. A human note on a need-input card is
    the answer, so it sends the card back to ready (AB-10).
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
        _release_answered(s, issue, actor, actor_kind, now)
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
         closes: bool = False, name: str | None = None, token: str | None = None,
         request_id: str | None = None) -> dict:
    """Attach a commit, PR, path or URL. `closes` marks it as finishing the issue.

    A url may carry a `name`, which the card shows in place of its host (AB-43).
    A blank name is no name.
    """
    if kind not in ARTIFACT_KINDS:
        raise BoardError(f"artifact kind {kind!r} is not one of {sorted(ARTIFACT_KINDS)}")
    name = (name or "").strip() or None
    if name is not None and kind != "url":
        raise BoardError(f"only a url artifact takes a name, not a {kind}")
    if name is not None and len(name) > ARTIFACT_NAME_MAX:
        raise BoardError(f"an artifact name is at most {ARTIFACT_NAME_MAX} characters")
    # An unnamed link hashes as it did before AB-43, so a retry spanning the
    # upgrade still collapses to one event.
    named = [] if name is None else [name]
    key = (_scoped("link", issue_id, request_id)
           or _derive(actor, "link", issue_id, token, kind, ref, closes, *named))

    def body(s):
        issue = _get(s, issue_id, lock=True)
        now = store.db_now(s)
        if actor_kind == "agent" and issue.lease_holder is None:
            raise LeaseLost(f"{issue.id}: an agent links an artifact only under a lease")
        _guard(s, issue, actor, actor_kind, token, now, False, key)
        s.add(Artifact(issue_id=issue.id, kind=kind, ref=ref, name=name, closes=closes,
                       added_at=now, added_by=actor))
        issue.version += 1
        issue.updated_at = now
        note = (f"{kind} {ref}" + (f" ({name})" if name else "")
                + (" (closes)" if closes else ""))
        _event(s, issue.id, now, actor, actor_kind, "link", None, None, note, key)
        return _view(s, issue)

    return _write(engine, key, body, _replay_view,
                  [kind, ref, closes, actor, actor_kind, token, *named])


def create(engine, project: str, title: str, *, actor: str, actor_kind: str,
           body: str = "", rank: int | None = None,
           labels: list[str] = (), workflow_id: int | None = None,
           position: int | None = None, request_id: str | None = None,
           attachments: list[dict] = (), stage: str | None = None,
           queue: str | None = None, schedule: str | None = None,
           timezone: str | None = None, creator: str | None = None) -> dict:
    """A new issue, in `stage` (backlog when not given) and `queue`.

    `schedule` is five cron fields read in `timezone`, an IANA zone the
    caller resolves (AB-54); the two go together. `creator` is the person
    an agent enters the card for (AB-63); only an agent may name one.
    """
    where = _create_where("an issue", stage or "backlog", queue)
    creator = _check_creator(creator, actor, actor_kind)
    schedule, timezone = _check_schedule(schedule, timezone)
    if (workflow_id is None) != (position is None):
        raise BoardError("workflow_id and position go together")

    key = _scoped("create", project, request_id)

    def run(s):
        now = store.db_now(s)
        issue = _create(s, project, title, actor, actor_kind, body, where, rank,
                        labels, workflow_id, position, now, key, creator)
        if schedule is not None:
            _apply_schedule(issue, schedule, timezone, now)
        _attach(s, issue, None, attachments, actor, now)
        return _view(s, issue)

    return _write(engine, key, run, _replay_view,
                  [title, body, list(where), rank, sorted(set(labels)), workflow_id, position,
                   actor, actor_kind, schedule, timezone, *_digests(attachments),
                   # Only when given, so a call without one hashes as before AB-63.
                   *([["creator", creator]] if creator else [])])


def _check_creator(creator, actor, actor_kind):
    """The person a card is entered for, or None (AB-63, plan section 3).

    Naming one grants nothing, but it must still name a person: an email
    that is not a service account's, and not the agent itself.
    """
    if creator is None:
        return None
    if actor_kind != "agent":
        raise BoardError("only an agent may name a card's creator; "
                         "a person who creates a card is its creator")
    creator = creator.strip()
    if not creator or len(creator) > 200:
        raise BoardError("a creator is a non-empty email of at most 200 characters")
    who = creator.lower()
    if "@" not in who or who.endswith(SERVICE_ACCOUNT_SUFFIX):
        raise BoardError(f"a creator is a person's email, not {creator!r}")
    if who == actor.lower():
        raise BoardError("an agent cannot name itself as a card's creator")
    return creator


def set_schedule(engine, issue_id: str, schedule: str | None, *, timezone: str | None,
                 actor: str, actor_kind: str, token: str | None = None,
                 request_id: str | None = None, preempt: bool = False,
                 expected_version: int | None = None) -> dict:
    """Set, change or clear an issue's cron schedule and its zone (AB-54).

    Either change recomputes `next_run_at`; a None `schedule` clears all
    three. An agent changes a schedule only under its lease, as it moves a
    card. `expected_version` works as in `move`.
    """
    schedule, timezone = _check_schedule(schedule or None, timezone or None)
    key = (_scoped("schedule", issue_id, request_id)
           or _derive(actor, "schedule", issue_id, token, schedule, timezone))

    def body(s):
        issue = _get(s, issue_id, lock=True)
        if expected_version is not None and issue.version != expected_version:
            raise Conflict(f"{issue.id} is at version {issue.version}, "
                           f"the page showed {expected_version}")
        now = store.db_now(s)
        if actor_kind == "agent" and issue.lease_holder is None:
            raise LeaseLost(f"{issue.id}: an agent changes a schedule only under a lease")
        _guard(s, issue, actor, actor_kind, token, now, preempt, None)
        _apply_schedule(issue, schedule, timezone, now)
        issue.version += 1
        issue.updated_at = now
        note = (f"schedule {schedule} in {timezone}; next due {_iso(issue.next_run_at)}"
                if schedule else "schedule cleared")
        _event(s, issue.id, now, actor, actor_kind, SCHEDULE_EVENT, None, None, note, key)
        return _view(s, issue)

    return _write(engine, key, body, _replay_view,
                  [schedule, timezone, actor, actor_kind, token, preempt, expected_version])


def switch_schedule(engine, issue_id: str, on: bool, *, actor: str, actor_kind: str,
                    token: str | None = None, request_id: str | None = None,
                    preempt: bool = False, expected_version: int | None = None) -> dict:
    """Switch a card's schedule off or on, keeping its cron and zone (AB-65).

    Off empties `next_run_at`, so `next` never claims the card and a finished
    run waits in ready without coming due. On resumes from the next due time
    after now; fires missed while off are not made up. Guarded as
    `set_schedule` is.
    """
    key = (_scoped("schedule-switch", issue_id, request_id)
           or _derive(actor, "schedule-switch", issue_id, token, on))

    def body(s):
        issue = _get(s, issue_id, lock=True)
        if expected_version is not None and issue.version != expected_version:
            raise Conflict(f"{issue.id} is at version {issue.version}, "
                           f"the page showed {expected_version}")
        if not issue.schedule:
            raise BoardError(f"{issue.id} has no schedule to switch")
        now = store.db_now(s)
        if actor_kind == "agent" and issue.lease_holder is None:
            raise LeaseLost(f"{issue.id}: an agent switches a schedule only under a lease")
        _guard(s, issue, actor, actor_kind, token, now, preempt, None)
        issue.schedule_paused = not on
        _apply_schedule(issue, issue.schedule, issue.schedule_tz, now)
        issue.version += 1
        issue.updated_at = now
        note = (f"schedule switched on; next due {_iso(issue.next_run_at)}" if on
                else "schedule switched off")
        _event(s, issue.id, now, actor, actor_kind, SCHEDULE_EVENT, None, None, note, key)
        return _view(s, issue)

    return _write(engine, key, body, _replay_view,
                  [on, actor, actor_kind, token, preempt, expected_version])


def default_timezone() -> str:
    """The zone a CLI or MCP schedule is read in without an explicit one.

    An agent has no browser preferences, so it is `BOARD_TIMEZONE`, else UTC.
    """
    return os.environ.get(TZ_ENV) or "UTC"


def next_due(schedule: str, zone: str, after: datetime) -> datetime:
    """The first time strictly after `after` that `schedule` fires, in UTC.

    The fields are read as wall-clock times in `zone`, so a Sydney card
    follows daylight saving.
    """
    local = after.astimezone(ZoneInfo(zone))
    return croniter(schedule, local).get_next(datetime).astimezone(timezone.utc)


def instantiate(engine, template: str, project: str, *, actor: str, actor_kind: str,
                title: str | None = None, request_id: str | None = None,
                creator: str | None = None) -> dict:
    """Create a workflow and one ready issue per template step, all or nothing.

    `creator` names the person an agent makes the steps for, as in `create`
    (AB-69).
    """
    creator = _check_creator(creator, actor, actor_kind)
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
            _create(s, project, step.title, actor, actor_kind, step.body, READY, None,
                    (), wf.id, step.position, now, key if i == 0 else None, creator)
        s.flush()
        s.refresh(wf)
        return _workflow_view(s, wf)

    return _write(engine, key, run, replay, [project, title, actor, actor_kind,
                                             # Only when given, as in `create`.
                                             *([["creator", creator]] if creator else [])])


def create_batch(engine, items: list[dict], *, actor: str, actor_kind: str,
                 workflow_title: str | None = None,
                 request_id: str | None = None, creator: str | None = None) -> dict:
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
    ref to its id, and `workflow_id`. `creator` names the person an agent
    makes the batch for, as in `create`; an item's own `creator` overrides
    it (AB-69).
    """
    items = [_batch_item(i, it) for i, it in enumerate(items)]
    creator = _check_creator(creator, actor, actor_kind)
    for it in items:
        it["creator"] = _check_creator(it["creator"], actor, actor_kind)
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
                                  it["where"], it["rank"], it["labels"],
                                  wf and wf.id, wf and i + 1, now, item_key,
                                  it["creator"] or creator))
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

    # A creator is hashed only when given, as in `create`, so a call without
    # one hashes as before AB-69.
    return _write(engine, key, body, replay,
                  [[{k: it[k] for k in ("project", "title", "body", "where", "rank",
                                        "labels", "ref", "after")}
                    | ({"creator": it["creator"]} if it["creator"] else {})
                    for it in items],
                   workflow_title, actor, actor_kind,
                   *([["creator", creator]] if creator else [])])


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
    if "state" in it:
        raise BoardError(f"item {i}: state is gone (AB-55); give a stage")
    where = list(_create_where(f"item {i}: an issue", it.get("stage") or "implement",
                               it.get("queue")))
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
            "where": where, "rank": rank, "labels": sorted({l.strip() for l in labels}),
            "ref": ref, "after": after, "creator": it.get("creator")}


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
    Both moves are outside STAGE_MOVES and QUEUE_MOVES on purpose. An agent needs its live
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
        # An agent's steps are for the person the origin was made for (AB-69);
        # a person who plans is the steps' creator, as in `create`.
        made = _creator_view(issue)
        creator = (made["creator"] if actor_kind == "agent"
                   and made["creator_kind"] == "human" else None)
        for position, st in enumerate(steps, 1):
            # The origin's rank, so planning an urgent issue keeps its steps urgent.
            _create(s, st["project"] or issue.project_key, st["title"], actor, actor_kind,
                    st["body"], READY, issue.rank, (), wf.id, position, now, None, creator)
        old_stage, old_queue = issue.state, issue.queue
        if issue.state not in WORK_STAGES:
            issue.state = "implement"
        issue.queue = "on-hold"
        issue.lease_holder = issue.lease_token = issue.lease_expires_at = None
        issue.version += 1
        issue.updated_at = now
        n = len(steps)
        _event(s, issue.id, now, actor, actor_kind, "transition", old_stage, issue.state,
               f"planned as workflow {wf.id}: {n} step{'' if n == 1 else 's'}",
               None if _has_key(s, key) else key, old_queue, "on-hold")
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
         labels: list[str] | None = None, request_id: str | None = None,
         attachments: list[dict] = ()) -> dict:
    """A change from the web UI (design section 6).

    Under someone else's lease this raises `LeaseHeld` unless `preempt`. On
    preempt the lease moves to the actor with no expiry and the issue stays
    processing until an explicit release (Chaos's decision 1, section 11).
    `expected_version` is the version the form was rendered from. Saving a
    form that changes nothing writes nothing and returns the issue. Only a
    human or the system edits; an agent works under its lease through
    `queue`, `annotate` and `link`. A human save that changes a waiting
    card sends it back to ready (AB-10). A stage or queue change is `move`
    or `queue`, not an edit (AB-56).
    """
    if actor_kind == "agent":
        raise BoardError(f"{issue_id}: an agent cannot edit; it changes an issue under "
                         "its lease through queue, annotate or link")

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
        if attachments:
            _attach(s, issue, None, attachments, actor, now)
            changed.append("attachments")
        if changed:
            issue.version += 1
            issue.updated_at = now
            key = None if _has_key(s, request_id) else request_id
            _event(s, issue.id, now, actor, actor_kind, "edit", None, None,
                   "changed " + ", ".join(changed), key)
            _release_answered(s, issue, actor, actor_kind, now)
        return _view(s, issue)

    return _write(engine, request_id, run, _replay_view,
                  [expected_version, preempt, title, body, rank,
                   sorted(set(labels)) if labels is not None else None,
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
                and_(Issue.queue == "ready", Issue.schedule_paused.is_(False),
                     or_(Issue.next_run_at.is_(None), Issue.next_run_at <= now)),
                and_(Issue.queue == "processing",
                     Issue.lease_expires_at.is_not(None),
                     Issue.lease_expires_at < now),
            ),
            ~exists().where(Artifact.issue_id == Issue.id, Artifact.closes.is_(True)),
            ~exists().where(Dependency.issue_id == Issue.id,
                            blocker.id == Dependency.depends_on_id,
                            blocker.state != "done",
                            ~_started_after_done(Issue, blocker)),
            or_(
                Issue.workflow_id.is_(None),
                # A step of a workflow that declares dependencies waits only
                # on its own (MS-646); otherwise the workflow is strict.
                exists().where(sibling.workflow_id == Issue.workflow_id,
                               Dependency.issue_id == sibling.id),
                ~exists().where(earlier.workflow_id == Issue.workflow_id,
                                earlier.position < Issue.position,
                                earlier.state != "done",
                                ~_started_after_done(Issue, earlier)),
            ),
        )
        .order_by(Issue.rank, Issue.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=Issue)
    )
    if project is not None:
        q = q.where(Issue.project_key == project)
    return q


def _started_after_done(issue, blocker):
    """Whether `issue` was claimed after `blocker` last went done (AB-24).

    A reopened blocker holds back only the issues that have not started on
    the strength of it. Events are compared by id: imported ones share an `at`.
    Nested two levels deep, it must correlate `issue` explicitly, or the
    subquery lists the issue table afresh and matches any claim at all.
    """
    claimed, arrived = aliased(Event), aliased(Event)
    return exists().where(
        claimed.issue_id == issue.id, claimed.kind.in_(("claim", "reclaim")),
        arrived.issue_id == blocker.id, arrived.to_state == "done",
        or_(arrived.from_state.is_(None), arrived.from_state != "done"),
        claimed.id > arrived.id).correlate_except(claimed, arrived)


def _create_where(what, stage, queue):
    """The stage and queue status a new issue starts in.

    A work stage starts queued as ready; any other stage starts unqueued.
    """
    if stage not in CREATE_STAGES:
        raise InvalidTransition(f"{what} is created in {sorted(CREATE_STAGES)}, not {stage!r}")
    allowed = {None, "ready"} if stage in WORK_STAGES else {None}
    if queue not in allowed:
        raise InvalidTransition(f"{what} in {stage} is not created {queue!r}")
    return stage, ("ready" if stage in WORK_STAGES else None)


def _create(s, project, title, actor, actor_kind, body, where, rank, labels,
            workflow_id, position, now, key, creator=None):
    stage, queue = where
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
                  body=body, state=stage, queue=queue, rank=rank, workflow_id=workflow_id,
                  position=position, created_at=now, updated_at=now,
                  labels=[Label(name=n) for n in sorted(set(labels))])
    s.add(issue)
    s.flush()
    _event(s, issue.id, now, actor, actor_kind, "create", None, stage, None, key, None, queue,
           on_behalf_of=creator)
    return issue


def _check_schedule(schedule, zone):
    """A valid (schedule, zone) pair, or (None, None) when there is no schedule.

    An unknown zone is refused rather than skipped: a schedule read in the
    wrong zone fires at the wrong hour.
    """
    if schedule is None:
        return None, None
    schedule = " ".join(schedule.split())
    if len(schedule.split()) != 5 or not croniter.is_valid(schedule):
        raise BoardError(f"a schedule is five cron fields, not {schedule!r}")
    if any(RANDOM_ITEM.match(item) for field in schedule.split() for item in field.split(",")):
        raise BoardError(f"the schedule {schedule!r} has a random R field, so its runs could "
                         f"fall under {SCHEDULE_MIN_GAP} minutes apart")
    if not zone:
        raise BoardError("a schedule needs a time zone")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise BoardError(f"unknown time zone {zone!r}") from None
    try:
        # croniter accepts a date that never comes, such as 31 February.
        next_due(schedule, zone, datetime.now(timezone.utc))
    except CroniterBadDateError:
        raise BoardError(f"the schedule {schedule!r} never comes due") from None
    gap = _short_gap(schedule)
    if gap is not None:
        raise BoardError(f"the schedule {schedule!r} can fire as little as {gap} "
                         f"minute{'s' * (gap != 1)} apart; runs must be at least "
                         f"{SCHEDULE_MIN_GAP} minutes apart")
    return schedule, zone


def _short_gap(schedule):
    """The fewest minutes between two fire times, when under SCHEDULE_MIN_GAP.

    Gaps are wall-clock. Within a day they come from the minute and hour
    fields alone. Across midnight the day's last time meets the next day's
    first, which counts only when two consecutive dates both fire.
    """
    fields, _ = croniter.expand(schedule)
    minutes = range(60) if fields[0] == ["*"] else fields[0]
    hours = range(24) if fields[1] == ["*"] else fields[1]
    times = sorted(h * 60 + m for h in hours for m in minutes)
    gaps = [b - a for a, b in zip(times, times[1:])]
    overnight = 24 * 60 - times[-1] + times[0]
    if overnight < SCHEDULE_MIN_GAP and _fires_on_consecutive_days(schedule):
        gaps.append(overnight)
    gap = min(gaps, default=None)
    return gap if gap is not None and gap < SCHEDULE_MIN_GAP else None


def _fires_on_consecutive_days(schedule):
    days = croniter("0 0 " + " ".join(schedule.split()[2:]), datetime(2000, 1, 1))
    end = datetime(2000 + CALENDAR_CYCLE_YEARS, 1, 1)
    prev = days.get_next(datetime)
    while prev < end:
        day = days.get_next(datetime)
        if day - prev == timedelta(days=1):
            return True
        prev = day
    return False


def _apply_schedule(issue, schedule, zone, now):
    issue.schedule, issue.schedule_tz = schedule, zone
    # Clearing the schedule drops its switch; a switched-off one is never due.
    issue.schedule_paused = bool(schedule) and issue.schedule_paused
    issue.next_run_at = (next_due(schedule, zone, now)
                         if schedule and not issue.schedule_paused else None)


def _judge(issue, stage, queue, note, actor_kind):
    """Plan section 2's rules for a `move` or a `queue` call."""
    old_stage, old_queue = issue.state, issue.queue
    if stage != old_stage:
        if stage not in STAGE_MOVES[old_stage]:
            raise InvalidTransition(f"{issue.id}: {old_stage} -> {stage} is not allowed")
        if stage == "done" and not note and actor_kind != "human":
            raise NoteRequired(f"{issue.id}: a move into done needs a note")
        if old_stage == "done" and not note:
            raise NoteRequired(f"{issue.id}: reopening a done card needs a reason")
    elif queue != old_queue:
        if stage not in WORK_STAGES:
            raise InvalidTransition(f"{issue.id} is {stage}; only a planning or implement "
                                    "card is queued")
        if queue not in QUEUE_MOVES[old_queue]:
            raise InvalidTransition(f"{issue.id}: queue {old_queue or 'empty'} -> "
                                    f"{queue or 'empty'} is not allowed")
        if old_queue == "processing" and queue is None and not note:
            raise NoteRequired(f"{issue.id}: releasing a card from the queue needs a note")


def _set_place(s, issue, stage, queue, note, actor, actor_kind, now, key, key_used):
    """Write a card's stage and queue status, and one transition event.

    Leaving processing drops the lease, and entering ready lifts any closing
    artifact (AB-68). Closing a step may resume its plan's origin, and leaving
    done reopens the card.
    """
    old_stage, old_queue = issue.state, issue.queue
    if stage == "done" and issue.schedule and old_stage in WORK_STAGES:
        # Plan question 3: a finished run waits for its next due time.
        stage, queue = old_stage, "ready"
    if (stage, queue) != (old_stage, old_queue):
        issue.state, issue.queue = stage, queue
        if old_queue == "processing":
            issue.lease_holder = issue.lease_token = issue.lease_expires_at = None
        if queue == "ready" and old_queue != "ready":
            _unclose(issue)
        issue.version += 1
        issue.updated_at = now
    ev = _event(s, issue.id, now, actor, actor_kind, "transition", old_stage, stage, note,
                None if key_used else key, old_queue, queue)
    if stage == "done" and old_stage != "done":
        _resume_origin(s, issue, now)
    if old_stage == "done" and stage != "done":
        _reopen(s, issue, actor, now)
    return ev


def _unclose(issue):
    """A card back in ready has work left, so no artifact marks it finished (AB-68).

    `next` skips a card with a closing artifact, in `_candidates`. The
    artifacts stay linked; they only stop closing.
    """
    for a in issue.artifacts:
        a.closes = False


def _reopen(s, issue, actor, now):
    """What reopening a done card undoes beyond its own state (AB-24).

    A closing artifact would hide the card from `next`, so it stops closing;
    the artifact stays. A reopened step sends a plan origin that came back
    to `ready` and nobody has claimed since back to `onhold`, to wait for
    the step again; any other origin stays where it is, with a note.
    Dependents need nothing here: `next` judges them, in `_candidates`.
    """
    _unclose(issue)
    wf = issue.workflow
    if wf is None or wf.origin_issue_id is None:
        return
    origin = _get(s, wf.origin_issue_id, lock=True)
    note = f"step {issue.id} of workflow {wf.id} was reopened by {actor}"
    entered = s.scalars(select(Event)
                        .where(Event.issue_id == origin.id, Event.kind == "transition",
                               Event.to_queue == "ready")
                        .order_by(Event.id.desc()).limit(1)).first()
    # Resumed, not since claimed and handed back by a checkpoint.
    if origin.queue == "ready" and entered is not None and entered.from_queue == "on-hold":
        origin.queue = "on-hold"
        origin.version += 1
        origin.updated_at = now
        _event(s, origin.id, now, "board", "system", "transition", origin.state,
               origin.state, f"{note}; this waits for it again", None, "ready", "on-hold")
    elif origin.queue != "on-hold":
        _event(s, origin.id, now, "board", "system", "annotate", None, None,
               f"{note}; this stays {origin.state}, {origin.queue or 'not queued'}", None)


def _left_by(s, issue, actor, state):
    """Whether `actor`, an agent, may finish this need-input card without a lease.

    Entering need-input drops the lease, so a card an agent parked there for
    review before AB-23 could never be closed by it. The agent that made the
    latest move into need-input may still close or cancel it; nothing else.
    """
    if not _waiting(issue) or state not in {"done", "cancelled"}:
        return False
    last = s.scalars(select(Event)
                     .where(Event.issue_id == issue.id, Event.kind == "transition",
                            Event.to_state.in_(WORK_STAGES), Event.to_queue.is_(None))
                     .order_by(Event.id.desc()).limit(1)).first()
    return last is not None and last.actor == actor and last.actor_kind == "agent"


def _release_answered(s, issue, actor, actor_kind, now):
    """A human's note or save on a need-input card answers it: back to ready (AB-10).

    Only a human: an agent noting its own question must not release it. The
    caller's event already holds the idempotency key, so this one goes without.
    """
    if actor_kind == "human" and _waiting(issue):
        _set_place(s, issue, issue.state, "ready", None, actor, actor_kind, now, None,
                   key_used=True)


def _waiting(issue):
    """A work-stage card out of the queue: the old need-input."""
    return issue.state in WORK_STAGES and issue.queue is None


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
    if origin.queue != "on-hold":
        return
    origin.queue = "ready"
    _unclose(origin)
    origin.version += 1
    origin.updated_at = now
    _event(s, origin.id, now, "board", "system", "transition", origin.state, origin.state,
           f"every step of workflow {wf.id} is done; check the result and close this",
           None, "on-hold", "ready")


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
           f"lease taken over from {prev}", None if _has_key(s, key) else key,
           issue.queue, issue.queue)


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


def _event(s, issue_id, at, actor, actor_kind, kind, from_state, to_state, note, key,
           from_queue=None, to_queue=None, on_behalf_of=None):
    if actor_kind not in ACTOR_KINDS:
        raise BoardError(f"actor kind {actor_kind!r} is not one of {sorted(ACTOR_KINDS)}")
    ev = Event(issue_id=issue_id, at=at, actor=actor, actor_kind=actor_kind, kind=kind,
               from_state=from_state, to_state=to_state, from_queue=from_queue,
               to_queue=to_queue, on_behalf_of=on_behalf_of, note=note,
               idempotency_key=key)
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
        "stage": issue.state,
        "queue": issue.queue,
        "schedule": issue.schedule,
        "schedule_tz": issue.schedule_tz,
        "next_run_at": _iso(issue.next_run_at),
        "schedule_paused": issue.schedule_paused,
        "rank": issue.rank,
        "assignee": issue.assignee,
        "labels": [l.name for l in issue.labels],
        "lease_holder": issue.lease_holder,
        "lease_expires_at": _iso(issue.lease_expires_at),
        "version": issue.version,
        "created_at": _iso(issue.created_at),
        "updated_at": _iso(issue.updated_at),
        **_creator_view(issue),
        "closed_by_artifact": any(a.closes for a in issue.artifacts),
        "artifacts": [
            {"kind": a.kind, "ref": a.ref, "name": a.name, "closes": a.closes,
             "added_by": a.added_by,
             "added_at": _iso(a.added_at)}
            for a in issue.artifacts
        ],
        "attachments": [_attachment_view(a) for a in issue.attachments],
        "events": [
            {"id": e.id, "at": _iso(e.at), "actor": e.actor, "actor_kind": e.actor_kind,
             "kind": e.kind, "from_stage": e.from_state, "to_stage": e.to_state,
             "from_queue": e.from_queue, "to_queue": e.to_queue,
             "on_behalf_of": e.on_behalf_of, "note": e.note,
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


def _creator_view(issue):
    """Who a card was made for and which agent entered it (AB-63).

    `created_by_agent` is set only when an agent entered the card for a
    person; a creator named that way is a person, so human.
    """
    creates = [e for e in issue.events if e.kind == "create"]
    if not creates:
        return {"creator": None, "creator_kind": None, "created_by_agent": None}
    create = creates[0]
    if create.on_behalf_of:
        return {"creator": create.on_behalf_of, "creator_kind": "human",
                "created_by_agent": create.actor}
    return {"creator": create.actor, "creator_kind": create.actor_kind,
            "created_by_agent": None}


def _linked(s, far, near, issue_id):
    """The issues at the `far` end of this issue's dependency rows, in id order."""
    rows = s.execute(select(Issue.id, Issue.title, Issue.state, Issue.queue)
                     .join(Dependency, Issue.id == far).where(near == issue_id)
                     .order_by(Issue.id))
    return [{"id": i, "title": t, "stage": st, "queue": q} for i, t, st, q in rows]


def _workflow_place(steps):
    """PRD section 5, as a (stage, queue) pair: done when every step is done.

    A workflow waits out of the queue when any step does, and is processing
    once any step is processing or done. In a strict workflow every step is
    done once the last one is; with declared dependencies (MS-646) the last
    position can finish first.

    A workflow nobody has started is ready when its first step is ready,
    because that is the step `next` would hand out, and backlog otherwise.
    """
    if _all_done(steps):
        return "done", None
    if any(st.state in WORK_STAGES and st.queue is None for st in steps):
        return "implement", None
    if any(st.state == "done" or st.queue == "processing" for st in steps):
        return "implement", "processing"
    if steps and steps[0].state in WORK_STAGES and steps[0].queue == "ready":
        return "implement", "ready"
    return "backlog", None


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
    stage, queue = _workflow_place(wf.steps)
    return {
        "id": wf.id,
        "title": wf.title,
        "origin_issue_id": wf.origin_issue_id,
        "stage": stage,
        "queue": queue,
        "steps": [{"id": st.id, "position": st.position, "title": st.title,
                   "stage": st.state, "queue": st.queue, "depends_on": deps[st.id]}
                  for st in wf.steps],
    }
