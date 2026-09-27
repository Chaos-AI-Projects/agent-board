"""The `board` command: a thin shell over board.core (design section 1).

A cron job gates on the exit code, so the codes are the contract:

    0  success, the result as JSON on stdout
    1  any other error: a board rule, the database, or BOARD_DATABASE_URL
    2  a usage error, printed by argparse as text
    3  `next` found an empty queue, the same code `backlog.py next` uses,
       or `search` matched nothing
    4  lease lost: the caller's lease token is no longer current
    5  conflict, including an issue held under someone else's lease

Every error but a usage error prints as one JSON object on stderr, naming
the error class. main() catches everything rather than a list of database
and driver errors, because a list misses the next spelling.

The actor comes from `--actor` or BOARD_ACTOR, before or after the
subcommand; `migrate`, `create-project`, `show` and `search` need none. `next`
always claims as an agent. Without `--request-id`, a write under a lease
gets the derived key from design section 8, which board.core computes
from the actor, issue, operation, arguments and token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta

from board import core, importer, store

ACTOR_ENV = "BOARD_ACTOR"

EXIT_ERROR = 1
EXIT_EMPTY = 3
EXIT_LEASE_LOST = 4
EXIT_CONFLICT = 5

MAX_TTL = timedelta(days=30)


def _minutes(value: str) -> timedelta:
    try:
        ttl = timedelta(minutes=float(value))
    except (ValueError, OverflowError):
        ttl = None
    if ttl is None or not timedelta(0) < ttl <= MAX_TTL:
        raise argparse.ArgumentTypeError(
            f"--ttl is minutes, above 0 and at most {MAX_TTL.days} days")
    return ttl


def build_parser() -> argparse.ArgumentParser:
    # The actor options are accepted on both sides of the subcommand. The
    # subcommand's copies default to SUPPRESS, so an absent one does not
    # overwrite a value given before it. They must be separate actions: a
    # parent parser shares its action objects, defaults included.
    def actor_options(parser, actor, kind):
        parser.add_argument("--actor", default=actor,
                            help=f"who is acting; defaults to ${ACTOR_ENV}")
        parser.add_argument("--actor-kind", default=kind, choices=sorted(core.ACTOR_KINDS))

    p = argparse.ArgumentParser(prog="board", description=__doc__.split("\n")[0])
    actor_options(p, os.environ.get(ACTOR_ENV), "agent")
    shared = argparse.ArgumentParser(add_help=False)
    actor_options(shared, argparse.SUPPRESS, argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)

    def op(name, **kw):
        return sub.add_parser(name, parents=[shared], **kw)

    def writes(sp, token=True):
        if token:
            sp.add_argument("--token", help="the lease token `next` returned")
        sp.add_argument("--request-id", help="names this call, so a retry replays it")

    op("migrate", help="apply the schema migrations")

    sp = op("create-project", help="a new project; exit 5 when the key is taken")
    sp.add_argument("key")
    sp.add_argument("name")

    sp = op("next", help="claim the first workable issue; exit 3 when there is none")
    sp.add_argument("--project")
    sp.add_argument("--ttl", type=_minutes, default=core.DEFAULT_TTL)
    writes(sp, token=False)

    sp = op("show", help="one issue with its events")
    sp.add_argument("id")

    sp = op("search", help="issues matching a query and filters; exit 3 when none match")
    sp.add_argument("q", nargs="?", help="a substring of the id, title, body or a label")
    sp.add_argument("--project")
    sp.add_argument("--label")
    sp.add_argument("--assignee")

    sp = op("transition", help="move an issue to another state")
    sp.add_argument("id")
    sp.add_argument("state")
    sp.add_argument("--note")
    sp.add_argument("--preempt", action="store_true")
    writes(sp)

    sp = op("annotate", help="append a note")
    sp.add_argument("id")
    sp.add_argument("--note", required=True)
    writes(sp)

    sp = op("link", help="attach a commit, PR, path or URL")
    sp.add_argument("id")
    sp.add_argument("--artifact", required=True)
    sp.add_argument("--kind", required=True, choices=sorted(core.ARTIFACT_KINDS))
    sp.add_argument("--closes", action="store_true")
    writes(sp)

    sp = op("create", help="a new issue")
    sp.add_argument("--project", required=True)
    sp.add_argument("--title", required=True)
    sp.add_argument("--body", default="")
    sp.add_argument("--state", default="backlog", choices=sorted(core.CREATE_STATES))
    sp.add_argument("--rank", type=int)
    sp.add_argument("--label", action="append", default=[])
    sp.add_argument("--workflow-id", type=int)
    sp.add_argument("--position", type=int)
    writes(sp, token=False)

    sp = op("instantiate", help="a workflow and its issues from a template")
    sp.add_argument("template")
    sp.add_argument("--project", required=True)
    sp.add_argument("--title")
    writes(sp, token=False)

    sp = op("heartbeat", help="extend the lease on a claimed issue")
    sp.add_argument("id")
    sp.add_argument("--token", required=True)
    sp.add_argument("--ttl", type=_minutes, default=core.DEFAULT_TTL)

    sp = op("import-backlog", help="one-shot import of brain's backlog/*.md")
    sp.add_argument("--backlog-py", default=importer.DEFAULT_BACKLOG_PY,
                    help="the backlog.py whose parser reads the files")
    sp.add_argument("--root", help="the backlog directory; defaults to backlog.py's own")

    return p


def _run(engine, a) -> dict | None:
    who = {"actor": a.actor, "actor_kind": a.actor_kind}
    match a.command:
        case "migrate":
            store.upgrade(engine)
            return {"migrated": True}
        case "create-project":
            return core.create_project(engine, a.key, a.name)
        case "next":
            return core.next(engine, a.actor, ttl=a.ttl, project=a.project,
                             request_id=a.request_id)
        case "show":
            return core.show(engine, a.id)
        case "search":
            return {"issues": core.search(engine, q=a.q, project=a.project, label=a.label,
                                          assignee=a.assignee)}
        case "transition":
            return core.transition(engine, a.id, a.state, note=a.note, token=a.token,
                                   request_id=a.request_id, preempt=a.preempt, **who)
        case "annotate":
            return core.annotate(engine, a.id, a.note, token=a.token,
                                 request_id=a.request_id, **who)
        case "link":
            return core.link(engine, a.id, a.artifact, kind=a.kind, closes=a.closes,
                             token=a.token, request_id=a.request_id, **who)
        case "create":
            return core.create(engine, a.project, a.title, body=a.body, state=a.state,
                               rank=a.rank, labels=a.label, workflow_id=a.workflow_id,
                               position=a.position, request_id=a.request_id, **who)
        case "instantiate":
            return core.instantiate(engine, a.template, a.project, title=a.title,
                                    request_id=a.request_id, **who)
        case "heartbeat":
            return core.heartbeat(engine, a.id, a.actor, a.token, ttl=a.ttl)
        case "import-backlog":
            return importer.import_backlog(engine, importer.load_backlog_module(a.backlog_py),
                                           a.root, **who)
    raise AssertionError(a.command)


def _fail(code: int, exc: Exception) -> int:
    print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.command not in ("migrate", "create-project", "show", "search") and not a.actor:
        parser.error(f"--actor is required, or set ${ACTOR_ENV}")
    if a.command == "next" and a.actor_kind != "agent":
        parser.error("next claims a lease, and only an agent holds one")
    engine = None
    try:
        engine = store.make_engine()
        result = _run(engine, a)
    except core.LeaseLost as exc:
        return _fail(EXIT_LEASE_LOST, exc)
    except core.Conflict as exc:
        return _fail(EXIT_CONFLICT, exc)
    except Exception as exc:
        return _fail(EXIT_ERROR, exc)
    finally:
        if engine is not None:
            engine.dispose()
    if a.command == "next" and result is None:
        print(json.dumps({"issue": None}))
        return EXIT_EMPTY
    if a.command == "search" and not result["issues"]:
        print(json.dumps(result))
        return EXIT_EMPTY
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
