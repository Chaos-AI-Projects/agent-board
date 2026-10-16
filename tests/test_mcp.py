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
from board.brain import BRAIN_DIR_ENV, Brain
from board.mcp_server import build_server


def place(view):
    """A view's stage and queue status, the pair the old single state folded."""
    return view["stage"], view["queue"]

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
    return core.create(engine, "MS", title, stage="implement", actor=CHAOS,
                       actor_kind="human")["id"]


def claimed(engine):
    issue_id = ready(engine)
    claim = ok(engine, "next")
    return issue_id, claim["lease_token"]


def test_the_tools_are_the_cli_operations(migrated, monkeypatch):
    monkeypatch.delenv(BRAIN_DIR_ENV, raising=False)
    names = {t.name for t in list_tools(migrated)}
    assert names == {"next", "show", "annotate", "link", "create",
                     "instantiate", "heartbeat", "plan", "depend", "undepend",
                     "create_batch", "search", "projects", "move", "queue", "schedule"}


def test_next_claims_as_the_server_actor_and_returns_the_token(migrated):
    issue_id = ready(board(migrated))
    out = ok(migrated, "next")
    assert out["issue"]["id"] == issue_id
    assert place(out["issue"]) == ("implement", "processing")
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


def test_queue_under_the_lease_token(migrated):
    issue_id, token = claimed(board(migrated))
    out = ok(migrated, "queue", id=issue_id, status=None, note="PR #600", token=token)
    assert (out["stage"], out["queue"]) == ("implement", None)
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


def test_link_takes_a_name_for_a_url(migrated):
    issue_id, token = claimed(board(migrated))
    out = ok(migrated, "link", id=issue_id, artifact="https://example.com/d", kind="url",
             name="Design doc", token=token)
    assert [(a["ref"], a["name"]) for a in out["artifacts"]] == [
        ("https://example.com/d", "Design doc")]


def test_create_makes_an_agent_authored_issue(migrated):
    board(migrated)
    out = ok(migrated, "create", project="MS", title="found a bug", body="details",
             labels=["bug"], rank=3)
    assert (out["title"], out["body"], out["labels"], out["stage"]) == (
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
    assert place(out) == ("implement", "on-hold")
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


def test_ab41_a_callable_actor_kind_is_resolved_on_every_call(migrated):
    # Over HTTP a human's token acts as a human, a service account's as an agent.
    board(migrated)
    kinds = iter(["human", "agent"])

    async def run():
        server = build_server(migrated, WORKER, kind=lambda: next(kinds))
        async with create_connected_server_and_client_session(server) as client:
            made = []
            for title in ("by a human", "by an agent"):
                result = await client.call_tool("create", {"project": "MS", "title": title})
                made.append(json.loads(result.content[0].text)["id"])
            return made

    by_human, by_agent = anyio.run(run)
    assert core.show(migrated, by_human)["events"][0]["actor_kind"] == "human"
    assert core.show(migrated, by_agent)["events"][0]["actor_kind"] == "agent"


def test_search_finds_issues_by_query_and_project(migrated):
    board(migrated)
    core.create_project(migrated, "AB", "agent-board")
    hit = ready(migrated, "fix the lease bug")
    ready(migrated, "unrelated")
    other = core.create(migrated, "AB", "lease docs", actor=CHAOS, actor_kind="human")["id"]
    assert [i["id"] for i in ok(migrated, "search", q="LEASE")["issues"]] == [hit, other]
    assert [i["id"] for i in ok(migrated, "search", q="lease", project="MS")["issues"]] == [hit]


def test_ab27_search_by_creator(migrated):
    mine = ready(board(migrated), "mine")
    core.create(migrated, "MS", "theirs", actor="overlord", actor_kind="agent")
    assert [i["id"] for i in ok(migrated, "search", creator=CHAOS)["issues"]] == [mine]



def test_ab70_search_by_involved(migrated):
    iid = core.create(board(migrated), "MS", "theirs", actor="overlord", actor_kind="agent")["id"]
    core.annotate(migrated, iid, "looking", actor=CHAOS, actor_kind="human")
    core.create(migrated, "MS", "untouched", actor="overlord", actor_kind="agent")
    assert [i["id"] for i in ok(migrated, "search", involved=CHAOS)["issues"]] == [iid]

def test_search_with_no_match_is_an_empty_result_not_an_error(migrated):
    ready(board(migrated))
    assert ok(migrated, "search", q="nothing like this") == {"issues": []}


def test_projects_lists_every_project_with_its_issue_count(migrated):
    ready(board(migrated))
    core.create_project(migrated, "AB", "agent-board")
    assert ok(migrated, "projects") == {"projects": [
        {"key": "AB", "name": "agent-board", "issues": 0},
        {"key": "MS", "name": "memory-solution", "issues": 1}]}


# --- the brain's KB and docs (AB-22) ---

BRAIN_TOOLS = {"brain_search", "brain_read", "brain_list"}


def brain_dir(tmp_path):
    root = tmp_path / "brain"
    (root / "kb/wiki").mkdir(parents=True)
    (root / "plans").mkdir()
    (root / "kb/wiki/index.md").write_text("# Index\n\nThe lease page.\n")
    (root / "plans/fatigue.md").write_text("# Fatigue\n\nA lease, once.\n")
    (root / "CLAUDE.md").write_text("# Secret\n\nlease\n")
    return root


def brain_call(root, tool, **arguments):
    async def run():
        server = build_server(None, WORKER, brain=Brain(root, doc_dirs=["plans"]))
        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool(tool, arguments)
        return result.isError, json.loads(result.content[0].text)

    return anyio.run(run)


def tool_names(server):
    async def run():
        async with create_connected_server_and_client_session(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    return anyio.run(run)


def test_brain_tools_are_absent_while_board_brain_dir_is_unset(migrated, monkeypatch):
    monkeypatch.delenv(BRAIN_DIR_ENV, raising=False)
    assert not tool_names(build_server(migrated, WORKER)) & BRAIN_TOOLS


def test_board_brain_dir_adds_the_brain_tools(migrated, monkeypatch, tmp_path):
    monkeypatch.setenv(BRAIN_DIR_ENV, str(brain_dir(tmp_path)))
    assert BRAIN_TOOLS <= tool_names(build_server(migrated, WORKER))


def test_brain_list_by_scope(tmp_path):
    root = brain_dir(tmp_path)
    assert brain_call(root, "brain_list", scope="docs") == (
        False, {"files": [{"path": "plans/fatigue.md", "title": "Fatigue"}]})
    is_error, out = brain_call(root, "brain_list")
    assert [f["path"] for f in out["files"]] == ["kb/wiki/index.md", "plans/fatigue.md"]


def test_brain_search_returns_ranked_hits_with_lines(tmp_path):
    is_error, out = brain_call(brain_dir(tmp_path), "brain_search", q="lease", scope="all")
    assert not is_error
    assert out["results"][0] == {"path": "kb/wiki/index.md", "title": "Index",
                                 "lines": [{"line": 3, "text": "The lease page."}]}
    assert [r["path"] for r in out["results"]] == ["kb/wiki/index.md", "plans/fatigue.md"]


def test_brain_read_returns_the_page(tmp_path):
    is_error, out = brain_call(brain_dir(tmp_path), "brain_read", path="kb/wiki/index.md")
    assert (is_error, out["text"]) == (False, "# Index\n\nThe lease page.\n")


def test_brain_read_outside_the_named_directories_is_code_1(tmp_path):
    is_error, out = brain_call(brain_dir(tmp_path), "brain_read", path="kb/wiki/../../CLAUDE.md")
    assert is_error
    assert (out["error"], out["code"]) == ("BrainError", 1)


def test_ab24_an_agent_reopens_a_done_card_without_a_token(migrated):
    issue_id, token = claimed(board(migrated))
    ok(migrated, "move", id=issue_id, stage="done", note="merged", token=token)
    err = failed(migrated, "move", id=issue_id, stage="implement")
    assert err["error"] == "NoteRequired"
    out = ok(migrated, "move", id=issue_id, stage="implement", note="regressed")
    assert place(out) == ("implement", "ready")
    assert out["events"][-1]["actor"] == WORKER


# --- AB-52: stage and queue -------------------------------------------------


def test_ab52_move_sets_the_stage_under_the_lease(migrated):
    issue_id, token = claimed(board(migrated))
    out = ok(migrated, "move", id=issue_id, stage="planning", token=token)
    assert (out["stage"], out["queue"]) == ("planning", "ready")


def test_ab52_move_into_done_without_a_note_is_code_1(migrated):
    issue_id, token = claimed(board(migrated))
    out = failed(migrated, "move", id=issue_id, stage="done", token=token)
    assert (out["error"], out["code"]) == ("NoteRequired", 1)


def test_ab52_queue_on_hold_and_out_of_the_queue(migrated):
    issue_id, token = claimed(board(migrated))
    out = ok(migrated, "queue", id=issue_id, status=None, token=token, note="a question")
    assert (out["stage"], out["queue"]) == ("implement", None)


def test_ab52_queue_refuses_processing(migrated):
    issue_id, token = claimed(board(migrated))
    out = failed(migrated, "queue", id=issue_id, status="processing", token=token)
    assert out["error"] == "InvalidTransition"


def test_ab52_create_takes_a_stage(migrated):
    board(migrated)
    out = ok(migrated, "create", project="MS", title="p", stage="planning")
    assert (out["stage"], out["queue"]) == ("planning", "ready")


def test_ab52_create_batch_items_take_a_stage(migrated):
    board(migrated)
    out = ok(migrated, "create_batch", items=[
        {"project": "MS", "title": "a", "stage": "backlog"},
        {"project": "MS", "title": "b", "stage": "planning"}])
    a, b = (core.show(migrated, i) for i in out["ids"])
    assert (a["stage"], a["queue"], b["stage"], b["queue"]) == (
        "backlog", None, "planning", "ready")


def test_create_and_schedule_take_a_timezone(migrated, monkeypatch):
    core.create_project(migrated, "MS", "memory-solution")
    monkeypatch.delenv("BOARD_TIMEZONE", raising=False)
    issue = ok(migrated, "create", actor=CHAOS, project="MS", title="nightly",
               stage="implement", schedule="0 9 * * *")
    assert issue["schedule_tz"] == "UTC"
    sydney = ok(migrated, "create", actor=CHAOS, project="MS", title="x", stage="implement",
                schedule="0 9 * * *", timezone="Australia/Sydney")
    assert sydney["schedule_tz"] == "Australia/Sydney"
    is_error, payload = call(migrated, "create", actor=CHAOS, project="MS", title="y",
                             schedule="0 9 * * *", timezone="Mars/Olympus")
    assert is_error and "zone" in payload["message"]
    is_error, payload = call(migrated, "create", actor=CHAOS, project="MS", title="z",
                             schedule="* * * * *")
    assert is_error and "5 minutes" in payload["message"]
    assert ok(migrated, "next") == {"issue": None}


def test_schedule_sets_and_clears_under_the_lease(migrated, monkeypatch):
    monkeypatch.setenv("BOARD_TIMEZONE", "Australia/Sydney")
    issue_id = ready(board(migrated))
    token = ok(migrated, "next")["lease_token"]
    got = ok(migrated, "schedule", id=issue_id, schedule="0 9 * * *", token=token)
    assert (got["schedule"], got["schedule_tz"]) == ("0 9 * * *", "Australia/Sydney")
    got = ok(migrated, "schedule", id=issue_id, schedule=None, token=token)
    assert (got["schedule"], got["schedule_tz"], got["next_run_at"]) == (None, None, None)


def test_ab55_transition_and_create_state_are_gone(migrated):
    tools = {t.name: t for t in list_tools(board(migrated))}
    assert "transition" not in tools
    assert "state" not in tools["create"].inputSchema["properties"]
    out = ok(migrated, "create", actor=CHAOS, project="MS", title="t")
    assert (out["stage"], out["queue"]) == ("backlog", None)
    is_error, payload = call(migrated, "create_batch", items=[
        {"project": "MS", "title": "a", "state": "ready"}])
    assert is_error and "stage" in payload["message"]


def raw_call(engine, tool, **arguments):
    """One tool call; returns (is_error, text), for errors that are SDK text."""

    async def run():
        async with create_connected_server_and_client_session(
                build_server(engine, WORKER)) as client:
            result = await client.call_tool(tool, arguments)
        return result.isError, result.content[0].text

    return anyio.run(run)


def test_ab55_every_tool_refuses_an_unknown_argument(migrated):
    # FastMCP drops an unknown argument by default, so a stale state=ready
    # made an unqueued backlog card that looked like success.
    board(migrated)
    is_error, text = raw_call(migrated, "create", project="MS", title="t", state="ready")
    assert is_error and "state" in text
    assert core.search(migrated, project="MS") == []
    is_error, text = raw_call(migrated, "next", projekt="MS")
    assert is_error and "projekt" in text


def test_ab63_create_takes_the_person_an_agent_enters_the_card_for(migrated):
    board(migrated)
    out = ok(migrated, "create", project="MS", title="his ask", creator=CHAOS)
    assert (out["creator"], out["creator_kind"], out["created_by_agent"]) == (
        CHAOS, "human", WORKER)
    assert core.search(migrated, creator=CHAOS)[0]["id"] == out["id"]


def test_ab63_an_agent_cannot_name_itself_creator(migrated):
    board(migrated)
    assert failed(migrated, "create", project="MS", title="t", creator=WORKER)["code"] == 1


def test_ab69_instantiate_and_create_batch_take_a_creator(migrated):
    board(migrated)
    core.create_template(migrated, "release", "Release", ["build"])
    wf = ok(migrated, "instantiate", template="release", project="MS", creator=CHAOS)
    batch = ok(migrated, "create_batch", items=[{"project": "MS", "title": "a"}],
               creator=CHAOS)
    made = [wf["steps"][0]["id"], *batch["ids"]]
    assert sorted(i["id"] for i in core.search(migrated, creator=CHAOS)) == sorted(made)
