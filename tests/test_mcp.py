"""The MCP server as an agent sees it: tool calls over a real client session.

Each test drives the server in-process through the SDK's memory transport,
so arguments go through the same schema validation and results through the
same serialisation a stdio client gets. An error comes back as a tool
result with isError set and the CLI's error class and exit code in its JSON.
"""

import json

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from board import core
from board.mcp_server import build_server

WORKER = "worker-1"
CHAOS = "owner@example.com"


def call(engine, tool, actor=WORKER, **arguments):
    """One tool call; returns (is_error, payload parsed from the text)."""

    async def run():
        server = build_server(engine, actor)
        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool(tool, arguments)
        return result.isError, json.loads(result.content[0].text)

    return anyio.run(run)


def ok(engine, tool, **arguments):
    is_error, payload = call(engine, tool, **arguments)
    assert not is_error, payload
    return payload


def failed(engine, tool, **arguments):
    is_error, payload = call(engine, tool, **arguments)
    assert is_error, payload
    return payload


def list_tools(engine):
    async def run():
        async with create_connected_server_and_client_session(
                build_server(engine, WORKER)) as client:
            return (await client.list_tools()).tools

    return anyio.run(run)


def board(engine):
    core.create_project(engine, "MS", "memory-solution")
    return engine


def ready(engine, title="item"):
    return core.create(engine, "MS", title, state="ready", actor=CHAOS,
                       actor_kind="human")["id"]


def claimed(engine):
    issue_id = ready(engine)
    claim = ok(engine, "next")
    return issue_id, claim["lease_token"]


def test_the_tools_are_the_cli_operations(migrated):
    names = {t.name for t in list_tools(migrated)}
    assert names == {"next", "show", "transition", "annotate", "link", "create",
                     "instantiate", "heartbeat", "plan", "depend", "undepend",
                     "create_batch", "search", "projects"}


def test_next_claims_as_the_server_actor_and_returns_the_token(migrated):
    issue_id = ready(board(migrated))
    out = ok(migrated, "next")
    assert out["issue"]["id"] == issue_id
    assert out["issue"]["state"] == "processing"
    assert out["issue"]["lease_holder"] == WORKER
    assert out["lease_token"]


def test_next_on_an_empty_queue_is_a_result_not_an_error(migrated):
    board(migrated)
    assert ok(migrated, "next") == {"issue": None}


def test_show_returns_the_issue_and_its_events(migrated):
    issue_id = ready(board(migrated), "shown")
    out = ok(migrated, "show", id=issue_id)
    assert out["title"] == "shown"
    assert [e["kind"] for e in out["events"]][0] == "create"


def test_transition_under_the_lease_token(migrated):
    issue_id, token = claimed(board(migrated))
    out = ok(migrated, "transition", id=issue_id, state="done", note="PR #600", token=token)
    assert out["state"] == "done"
    assert core.show(migrated, issue_id)["events"][-1]["actor"] == WORKER


def test_annotate_with_a_stale_token_is_lease_lost_code_4(migrated):
    issue_id, _ = claimed(board(migrated))
    err = failed(migrated, "annotate", id=issue_id, note="hi", token="stale")
    assert (err["error"], err["code"]) == ("LeaseLost", 4)
    assert err["message"]


def test_link_attaches_an_artifact(migrated):
    issue_id, token = claimed(board(migrated))
    out = ok(migrated, "link", id=issue_id, artifact="71207df", kind="commit",
             closes=True, token=token)
    assert [(a["kind"], a["ref"], a["closes"]) for a in out["artifacts"]] == [
        ("commit", "71207df", True)]


def test_create_makes_an_agent_authored_issue(migrated):
    board(migrated)
    out = ok(migrated, "create", project="MS", title="found a bug", body="details",
             labels=["bug"], rank=3)
    assert (out["title"], out["body"], out["labels"], out["state"]) == (
        "found a bug", "details", ["bug"], "backlog")
    assert core.show(migrated, out["id"])["events"][0]["actor_kind"] == "agent"


def test_instantiate_creates_the_workflow(migrated):
    board(migrated)
    core.create_template(migrated, "release", "Release", ["build", "ship"])
    out = ok(migrated, "instantiate", template="release", project="MS")
    assert [st["title"] for st in out["steps"]] == ["build", "ship"]


def test_heartbeat_moves_the_lease_expiry(migrated):
    issue_id, token = claimed(board(migrated))
    before = core.show(migrated, issue_id)["lease_expires_at"]
    out = ok(migrated, "heartbeat", id=issue_id, token=token, ttl_minutes=600)
    assert out["lease_expires_at"] > before


def test_a_request_id_reused_with_other_arguments_is_conflict_code_5(migrated):
    issue_id, token = claimed(board(migrated))
    ok(migrated, "annotate", id=issue_id, note="one", token=token, request_id="r1")
    err = failed(migrated, "annotate", id=issue_id, note="two", token=token, request_id="r1")
    assert (err["error"], err["code"]) == ("Conflict", 5)


def test_any_other_board_error_is_code_1(migrated):
    board(migrated)
    err = failed(migrated, "show", id="MS-999")
    assert (err["error"], err["code"]) == ("NotFound", 1)


def test_an_unusable_ttl_is_refused(migrated):
    issue_id, token = claimed(board(migrated))
    err = failed(migrated, "heartbeat", id=issue_id, token=token, ttl_minutes=0)
    assert (err["error"], err["code"]) == ("ValueError", 1)


def test_plan_breaks_the_claimed_issue_into_steps(migrated):
    board(migrated)
    issue_id, token = claimed(migrated)
    out = ok(migrated, "plan", id=issue_id, token=token,
             steps=[{"title": "design"}, {"title": "build", "body": "the code"}])
    assert out["state"] == "onhold"
    assert [s["title"] for s in out["plan"]["steps"]] == ["design", "build"]
    step = core.show(migrated, out["plan"]["steps"][1]["id"])
    assert step["body"] == "the code"
    assert step["events"][0]["actor"] == WORKER


def test_plan_without_the_lease_is_code_4(migrated):
    board(migrated)
    issue_id = ready(migrated)
    out = failed(migrated, "plan", id=issue_id, steps=[{"title": "a"}])
    assert (out["error"], out["code"]) == ("LeaseLost", 4)


def test_depend_holds_an_issue_back_until_its_dependency_is_done(migrated):
    first, second = ready(board(migrated), "first"), ready(migrated, "second")
    out = ok(migrated, "depend", id=first, on=second)
    assert [d["id"] for d in out["depends_on"]] == [second]
    assert core.show(migrated, first)["events"][-1]["actor"] == WORKER
    assert ok(migrated, "next")["issue"]["id"] == second
    assert ok(migrated, "next") == {"issue": None}


def test_undepend_removes_the_dependency(migrated):
    first, second = ready(board(migrated), "first"), ready(migrated, "second")
    ok(migrated, "depend", id=first, on=second)
    assert ok(migrated, "undepend", id=first, on=second)["depends_on"] == []


def test_a_dependency_cycle_is_code_1(migrated):
    first, second = ready(board(migrated), "first"), ready(migrated, "second")
    ok(migrated, "depend", id=first, on=second)
    err = failed(migrated, "depend", id=second, on=first)
    assert (err["error"], err["code"]) == ("BoardError", 1)


# --- create_batch (MS-647) ----------------------------------------------------


def test_create_batch_creates_a_parallel_workflow_as_the_server_actor(migrated):
    board(migrated)
    out = ok(migrated, "create_batch", workflow_title="Fan out",
             items=[{"project": "MS", "title": "design", "ref": "d"},
                    {"project": "MS", "title": "left", "after": ["d"]},
                    {"project": "MS", "title": "right", "after": [0]}])
    design, left, right = out["ids"]
    assert out["refs"] == {"d": design}
    assert core.show(migrated, left)["events"][0]["actor"] == WORKER
    assert [d["id"] for d in core.show(migrated, right)["depends_on"]] == [design]


def test_a_bad_create_batch_is_an_error_and_writes_nothing(migrated):
    board(migrated)
    err = failed(migrated, "create_batch",
                 items=[{"project": "MS", "title": "a"}, {"project": "NOPE", "title": "b"}])
    assert err["code"] == 1
    assert core.search(migrated) == []


def test_a_callable_actor_is_resolved_on_every_call(migrated):
    # Remote MCP (MS-649) serves many agents from one server: the actor is
    # whoever the request's token names, looked up per call.
    first, second = ready(board(migrated), "a"), ready(migrated, "b")
    actors = iter(["alice@example.com", "bob@example.com"])

    async def run():
        server = build_server(migrated, lambda: next(actors))
        async with create_connected_server_and_client_session(server) as client:
            await client.call_tool("next", {})
            await client.call_tool("next", {})

    anyio.run(run)
    holders = {i: core.show(migrated, i)["lease_holder"] for i in (first, second)}
    assert holders == {first: "alice@example.com", second: "bob@example.com"}
    assert core.show(migrated, first)["events"][-1]["actor_kind"] == "agent"


def test_search_finds_issues_by_query_and_project(migrated):
    board(migrated)
    core.create_project(migrated, "AB", "agent-board")
    hit = ready(migrated, "fix the lease bug")
    ready(migrated, "unrelated")
    other = core.create(migrated, "AB", "lease docs", actor=CHAOS, actor_kind="human")["id"]
    assert [i["id"] for i in ok(migrated, "search", q="LEASE")["issues"]] == [hit, other]
    assert [i["id"] for i in ok(migrated, "search", q="lease", project="MS")["issues"]] == [hit]


def test_search_with_no_match_is_an_empty_result_not_an_error(migrated):
    ready(board(migrated))
    assert ok(migrated, "search", q="nothing like this") == {"issues": []}


def test_projects_lists_every_project_with_its_issue_count(migrated):
    ready(board(migrated))
    core.create_project(migrated, "AB", "agent-board")
    assert ok(migrated, "projects") == {"projects": [
        {"key": "AB", "name": "agent-board", "issues": 0},
        {"key": "MS", "name": "memory-solution", "issues": 1}]}
