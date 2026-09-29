"""The `board-mcp` server: board.core as MCP tools, for an agent in a conversation.

It is the CLI over another transport (design section 1), so the tools are
the CLI's operations under the same names, minus `migrate`. `edit` stays
out too, because only a human or the system edits.

One server is one agent. The actor comes from BOARD_ACTOR when the server
starts, and every write is recorded as `agent`. The lease token is a tool
argument, as it is the CLI's `--token`.

A success returns the operation's dict as JSON. `next` on an empty queue
is a success, `{"issue": null}`, because an empty queue is not a fault. An
error comes back as a tool result with isError set, carrying the CLI's
error JSON plus the exit code the CLI would have used:

    {"error": "LeaseLost", "message": "...", "code": 4}

Code 4 is lease lost and 5 a conflict, `LeaseHeld` included. Everything
else is 1, as it is in the CLI. An argument that fails the tool's schema
is the SDK's own error text, the counterpart of the CLI's usage error.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from board import cli, core, store

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


def build_server(engine, actor: str) -> FastMCP:
    """A server whose tools act on `engine` as the agent `actor`."""
    mcp = FastMCP("agent-board")
    who = {"actor": actor, "actor_kind": "agent"}

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
        return run(lambda: core.next(engine, actor, ttl=_ttl(ttl_minutes), project=project,
                                     request_id=request_id))

    @mcp.tool()
    def show(id: str) -> CallToolResult:
        """One issue with its events and artifacts."""
        return run(core.show, engine, id)

    @mcp.tool()
    def transition(id: str, state: str, token: str | None = None, note: str | None = None,
                   request_id: str | None = None, preempt: bool = False) -> CallToolResult:
        """Move an issue to another state. `done` and `need-input` need a note."""
        return run(core.transition, engine, id, state, note=note, token=token,
                   request_id=request_id, preempt=preempt, **who)

    @mcp.tool()
    def annotate(id: str, note: str, token: str | None = None,
                 request_id: str | None = None) -> CallToolResult:
        """Append a note to an issue."""
        return run(core.annotate, engine, id, note, token=token, request_id=request_id, **who)

    @mcp.tool()
    def link(id: str, artifact: str, kind: str, closes: bool = False,
             token: str | None = None, request_id: str | None = None) -> CallToolResult:
        """Attach a commit, pr, path or url. `closes` marks it as finishing the issue."""
        return run(core.link, engine, id, artifact, kind=kind, closes=closes, token=token,
                   request_id=request_id, **who)

    @mcp.tool()
    def create(project: str, title: str, body: str = "", state: str = "backlog",
               rank: int | None = None, labels: list[str] | None = None,
               workflow_id: int | None = None, position: int | None = None,
               request_id: str | None = None) -> CallToolResult:
        """A new issue, `backlog` unless state says `ready`."""
        return run(core.create, engine, project, title, body=body, state=state, rank=rank,
                   labels=labels or [], workflow_id=workflow_id, position=position,
                   request_id=request_id, **who)

    @mcp.tool()
    def instantiate(template: str, project: str, title: str | None = None,
                    request_id: str | None = None) -> CallToolResult:
        """A workflow and one ready issue per step of the named template."""
        return run(core.instantiate, engine, template, project, title=title,
                   request_id=request_id, **who)

    @mcp.tool()
    def plan(id: str, steps: list[dict[str, str]], token: str | None = None,
             request_id: str | None = None) -> CallToolResult:
        """Break a claimed issue into a workflow of ready steps, in the order given.

        Each step is {"title", "body"?, "project"?}; project defaults to the
        issue's. The issue goes onhold, releasing the lease, and comes back
        ready when the last step is done, for you to check and close.
        """
        return run(core.plan, engine, id, steps, token=token, request_id=request_id, **who)

    @mcp.tool()
    def create_batch(items: list[dict], workflow_title: str | None = None,
                     request_id: str | None = None) -> CallToolResult:
        """Create several issues together, all or nothing.

        Each item is {"project", "title", "body"?, "state"? (default ready),
        "rank"?, "labels"?, "ref"?, "after"?}. `after` lists what the item
        waits on: another item by 0-based index or by its `ref`, or an
        existing issue id. With workflow_title the items become a new
        workflow in list order. In a workflow, one `after` on any item
        drops strict order for every step, so give each step the `after` it
        needs. Returns `ids` in order, `refs` and `workflow_id`.
        """
        return run(core.create_batch, engine, items, workflow_title=workflow_title,
                   request_id=request_id, **who)

    @mcp.tool()
    def depend(id: str, on: str, request_id: str | None = None) -> CallToolResult:
        """Make issue `id` wait until issue `on` is done; `next` skips it until then.

        Once any step of a workflow has a dependency, that workflow's steps
        wait only on their own dependencies, so steps without one run in
        parallel. A self-dependency or a cycle is refused.
        """
        return run(core.depend, engine, id, on, request_id=request_id, **who)

    @mcp.tool()
    def undepend(id: str, on: str, request_id: str | None = None) -> CallToolResult:
        """Remove the dependency of issue `id` on issue `on`."""
        return run(core.undepend, engine, id, on, request_id=request_id, **who)

    @mcp.tool()
    def heartbeat(id: str, token: str,
                  ttl_minutes: float = DEFAULT_TTL_MINUTES) -> CallToolResult:
        """Extend the lease on an issue this agent claimed."""
        return run(lambda: core.heartbeat(engine, id, actor, token, ttl=_ttl(ttl_minutes)))

    return mcp


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
