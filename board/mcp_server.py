"""The `board-mcp` server: board.core as MCP tools, for an agent in a conversation.

It is the CLI over another transport (design section 1), so the tools are
the CLI's operations under the same names, minus `migrate` and
`create-project`. `edit` stays out too, because only a human or the system
edits. `projects` is the one tool the CLI lacks (AB-6): an agent creating a
card needs the project keys, and over MCP it has no other way to learn them.

Over stdio one server is one agent, and the actor comes from BOARD_ACTOR
when the server starts; every write is recorded as `agent`. Over HTTP
(MS-649) the actor and its kind are callables, asked on every tool call for
whoever the request's token names, so a human's token writes as `human`
(AB-41). The lease token is a tool argument, as it is the CLI's `--token`.

A success returns the operation's dict as JSON. `next` on an empty queue
is a success, `{"issue": null}`, because an empty queue is not a fault. An
error comes back as a tool result with isError set, carrying the CLI's
error JSON plus the exit code the CLI would have used:

    {"error": "LeaseLost", "message": "...", "code": 4}

Code 4 is lease lost and 5 a conflict, `LeaseHeld` included. Everything
else is 1, as it is in the CLI. An argument that fails the tool's schema
is the SDK's own error text, the counterpart of the CLI's usage error.

With BOARD_BRAIN_DIR set, three read-only tools reach a brain checkout's
KB and docs (AB-22): `brain_search`, `brain_read` and `brain_list`. They
carry a `brain_` prefix because `search` already finds issues. Unset, the
tools are absent rather than failing; board.brain has the rest.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from typing import Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from board import cli, core, store
from board.brain import Brain

DEFAULT_TTL_MINUTES = core.DEFAULT_TTL.total_seconds() / 60


def _result(payload: dict, error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                          isError=error)


def _code(exc: Exception) -> int:
    if isinstance(exc, core.LeaseLost):
        return cli.EXIT_LEASE_LOST
    if isinstance(exc, core.Conflict):
        return cli.EXIT_CONFLICT
    return cli.EXIT_ERROR


def _ttl(minutes: float) -> timedelta:
    try:
        ttl = timedelta(minutes=minutes)
    except OverflowError:
        ttl = None
    if ttl is None or not timedelta(0) < ttl <= cli.MAX_TTL:
        raise ValueError(f"ttl_minutes is above 0 and at most {cli.MAX_TTL.days} days")
    return ttl


_FROM_ENV = object()


def build_server(engine, actor: str | Callable[[], str], brain=_FROM_ENV,
                 kind: str | Callable[[], str] = "agent") -> FastMCP:
    """A server whose tools act on `engine` as `actor` of kind `kind`, or as
    whoever `actor()` and `kind()` return at each call. `brain` defaults to
    Brain.from_env(), and None leaves the brain tools out."""
    mcp = FastMCP("agent-board")
    if brain is _FROM_ENV:
        brain = Brain.from_env()
    current = actor if callable(actor) else (lambda: actor)
    current_kind = kind if callable(kind) else (lambda: kind)

    def who() -> dict:
        return {"actor": current(), "actor_kind": current_kind()}

    def run(op, *args, **kwargs) -> CallToolResult:
        # One catch-all, like cli.main(): a list of database and driver
        # errors misses the next spelling.
        try:
            out = op(*args, **kwargs)
        except Exception as exc:
            return _result({"error": type(exc).__name__, "message": str(exc),
                            "code": _code(exc)}, error=True)
        return _result({"issue": None} if out is None else out)

    @mcp.tool()
    def next(project: str | None = None, ttl_minutes: float = DEFAULT_TTL_MINUTES,
             request_id: str | None = None) -> CallToolResult:
        """Claim the first workable issue and return it with its lease token.

        An empty queue returns {"issue": null}. Pass the token to every
        write on the issue until it is done.
        """
        return run(lambda: core.next(engine, current(), ttl=_ttl(ttl_minutes), project=project,
                                     request_id=request_id))

    @mcp.tool()
    def show(id: str) -> CallToolResult:
        """One issue with its events and artifacts."""
        return run(core.show, engine, id)

    @mcp.tool()
    def search(q: str | None = None, project: str | None = None, label: str | None = None,
               assignee: str | None = None, creator: str | None = None) -> CallToolResult:
        """Issues matching every filter given, in board order, without bodies.

        `q` is a case-insensitive substring of the id, title, body or any
        label. `project`, `label`, `assignee` and `creator` match exactly;
        `assignee` also matches the lease holder, and `creator` is whoever
        created the issue. No filter lists every issue. No match is
        {"issues": []}, not an error.
        """
        return run(lambda: {"issues": core.search(engine, q=q, project=project, label=label,
                                                  assignee=assignee, creator=creator)})

    @mcp.tool()
    def projects() -> CallToolResult:
        """Every project's key and name, with how many issues it holds."""
        return run(lambda: {"projects": core.projects(engine)})

    @mcp.tool()
    def move(id: str, stage: str, token: str | None = None, note: str | None = None,
             request_id: str | None = None, preempt: bool = False) -> CallToolResult:
        """Set an issue's stage: backlog, planning, implement, done or cancelled.

        Entering planning or implement queues the card as ready and releases
        the lease. An agent's `done` needs a note; a human may close without
        one. A `done` card reopens to implement with a note giving the
        reason, no lease needed.
        """
        return run(core.move, engine, id, stage, note=note, token=token,
                   request_id=request_id, preempt=preempt, **who())

    @mcp.tool()
    def queue(id: str, status: str | None, token: str | None = None,
              note: str | None = None, request_id: str | None = None,
              preempt: bool = False) -> CallToolResult:
        """Set a planning or implement card's queue status: ready, on-hold, or null.

        Null takes the card out of the queue and needs a note, as when it
        waits on a question. Only `next` sets processing.
        """
        return run(core.queue, engine, id, status, note=note, token=token,
                   request_id=request_id, preempt=preempt, **who())

    @mcp.tool()
    def annotate(id: str, note: str, token: str | None = None,
                 request_id: str | None = None) -> CallToolResult:
        """Append a note to an issue."""
        return run(core.annotate, engine, id, note, token=token, request_id=request_id, **who())

    @mcp.tool()
    def link(id: str, artifact: str, kind: str, closes: bool = False, name: str | None = None,
             token: str | None = None, request_id: str | None = None) -> CallToolResult:
        """Attach a commit, pr, path or url. `closes` marks it as finishing the issue.
        A url may take a `name`, shown on the card in place of its host."""
        return run(core.link, engine, id, artifact, kind=kind, closes=closes, name=name,
                   token=token, request_id=request_id, **who())

    @mcp.tool()
    def create(project: str, title: str, body: str = "", stage: str = "backlog",
               rank: int | None = None, labels: list[str] | None = None,
               workflow_id: int | None = None, position: int | None = None,
               request_id: str | None = None,
               queue: str | None = None, schedule: str | None = None,
               timezone: str | None = None) -> CallToolResult:
        """A new issue in `stage`: backlog, planning or implement.

        Planning and implement start queued as ready.
        `schedule` is five cron fields read in `timezone`, which defaults to
        $BOARD_TIMEZONE, else UTC; `next` skips the card until each due time.
        """
        return run(core.create, engine, project, title, body=body, stage=stage,
                   queue=queue, rank=rank,
                   labels=labels or [], workflow_id=workflow_id, position=position,
                   request_id=request_id, schedule=schedule,
                   timezone=_zone(schedule, timezone), **who())

    @mcp.tool()
    def schedule(id: str, schedule: str | None, timezone: str | None = None,
                 token: str | None = None, request_id: str | None = None,
                 preempt: bool = False) -> CallToolResult:
        """Set or clear (null) a card's cron schedule: five fields read in `timezone`.

        `timezone` defaults to $BOARD_TIMEZONE, else UTC; an unknown zone is
        refused. A change recomputes the next due time. A scheduled card's
        finished run returns to ready rather than done.
        """
        return run(core.set_schedule, engine, id, schedule,
                   timezone=_zone(schedule, timezone), token=token,
                   request_id=request_id, preempt=preempt, **who())

    @mcp.tool()
    def instantiate(template: str, project: str, title: str | None = None,
                    request_id: str | None = None) -> CallToolResult:
        """A workflow and one ready issue per step of the named template."""
        return run(core.instantiate, engine, template, project, title=title,
                   request_id=request_id, **who())

    @mcp.tool()
    def plan(id: str, steps: list[dict[str, str]], token: str | None = None,
             request_id: str | None = None) -> CallToolResult:
        """Break a claimed issue into a workflow of ready steps, in the order given.

        Each step is {"title", "body"?, "project"?}; project defaults to the
        issue's. The issue's queue goes on-hold, releasing the lease, and comes back
        ready when the last step is done, for you to check and close.
        """
        return run(core.plan, engine, id, steps, token=token, request_id=request_id, **who())

    @mcp.tool()
    def create_batch(items: list[dict], workflow_title: str | None = None,
                     request_id: str | None = None) -> CallToolResult:
        """Create several issues together, all or nothing.

        Each item is {"project", "title", "body"?, "stage"? (backlog, planning
        or implement; default implement, queued ready), "rank"?, "labels"?, "ref"?, "after"?}. `after` lists what the item
        waits on: another item by 0-based index or by its `ref`, or an
        existing issue id. With workflow_title the items become a new
        workflow in list order. In a workflow, one `after` on any item
        drops strict order for every step, so give each step the `after` it
        needs. Returns `ids` in order, `refs` and `workflow_id`.
        """
        return run(core.create_batch, engine, items, workflow_title=workflow_title,
                   request_id=request_id, **who())

    @mcp.tool()
    def depend(id: str, on: str, request_id: str | None = None) -> CallToolResult:
        """Make issue `id` wait until issue `on` is done; `next` skips it until then.

        Once any step of a workflow has a dependency, that workflow's steps
        wait only on their own dependencies, so steps without one run in
        parallel. A self-dependency or a cycle is refused.
        """
        return run(core.depend, engine, id, on, request_id=request_id, **who())

    @mcp.tool()
    def undepend(id: str, on: str, request_id: str | None = None) -> CallToolResult:
        """Remove the dependency of issue `id` on issue `on`."""
        return run(core.undepend, engine, id, on, request_id=request_id, **who())

    @mcp.tool()
    def heartbeat(id: str, token: str,
                  ttl_minutes: float = DEFAULT_TTL_MINUTES) -> CallToolResult:
        """Extend the lease on an issue this agent claimed."""
        return run(lambda: core.heartbeat(engine, id, current(), token, ttl=_ttl(ttl_minutes)))

    if brain is not None:
        _brain_tools(mcp, brain, run)
    _refuse_unknown_arguments(mcp)
    return mcp


def _refuse_unknown_arguments(mcp: FastMCP) -> None:
    """Make every tool's argument model refuse a name it does not declare.

    FastMCP's models ignore extras, so a caller still sending a removed
    argument, such as create's old `state`, got a success that silently
    did something else (AB-55). Each tool has its own generated model, so
    changing its config touches nothing shared.
    """
    for tool in mcp._tool_manager.list_tools():
        model = tool.fn_metadata.arg_model
        model.model_config["extra"] = "forbid"
        model.model_rebuild(force=True)


def _brain_tools(mcp: FastMCP, brain: Brain, run) -> None:
    @mcp.tool()
    def brain_search(q: str, scope: str = "all") -> CallToolResult:
        """Search the brain's markdown for files holding every word of `q`.

        Matching is case-insensitive substring, not semantic; browse by
        meaning from the KB's index.md instead. `scope` is kb, docs or all.
        Returns up to 20 results, title and filename hits first, each with
        path, title and up to 3 matching lines with their numbers.
        """
        return run(lambda: {"results": brain.search(q, scope)})

    @mcp.tool()
    def brain_read(path: str, scope: str = "all") -> CallToolResult:
        """One markdown file's text, by the path brain_list or brain_search gave.

        A path outside the scope's directories, or not ending .md, is refused.
        """
        return run(brain.read, path, scope)

    @mcp.tool()
    def brain_list(scope: str = "all") -> CallToolResult:
        """Every markdown file in the scope (kb, docs or all), with its title."""
        return run(lambda: {"files": brain.list(scope)})



def _zone(schedule: str | None, zone: str | None) -> str | None:
    """The zone a schedule is read in: the one given, else the default."""
    if schedule is None:
        return None
    return zone or core.default_timezone()


def main() -> int:
    actor = os.environ.get(cli.ACTOR_ENV)
    if not actor:
        print(f"board-mcp: set ${cli.ACTOR_ENV} to the agent this server acts as",
              file=sys.stderr)
        return 2
    engine = store.make_engine()
    try:
        build_server(engine, actor).run("stdio")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
