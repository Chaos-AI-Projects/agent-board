"""board.core against design section 10, rows 1-5 and 8, on both engines.

The PostgreSQL leg runs only when BOARD_TEST_PG_URL is set. Row 1 on SQLite
passes because BEGIN IMMEDIATE serialises the two callers, so it says nothing
about FOR UPDATE SKIP LOCKED; only the PostgreSQL leg tests that.
"""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from board import core, store

AGENT = "agent"
HUMAN = "human"
CHAOS = "owner@example.com"


@pytest.fixture
def board(migrated):
    core.create_project(migrated, "MS", "memory-solution")
    return migrated


def ready(engine, title="item", **kw):
    return core.create(engine, "MS", title, actor=CHAOS, actor_kind=HUMAN,
                       state="ready", **kw)["id"]


def events(engine, issue_id):
    return core.show(engine, issue_id)["events"]


# --- create, show, the queue ------------------------------------------------


def test_create_hands_out_sequential_ids_and_a_create_event(board):
    a = core.create(board, "MS", "first", actor=CHAOS, actor_kind=HUMAN)
    b = core.create(board, "MS", "second", actor=CHAOS, actor_kind=HUMAN)
    assert (a["id"], b["id"]) == ("MS-1", "MS-2")
    assert a["state"] == "backlog"
    assert [e["kind"] for e in events(board, "MS-1")] == ["create"]


def test_next_on_an_empty_queue_returns_none(board):
    core.create(board, "MS", "not authorized yet", actor=CHAOS, actor_kind=HUMAN)
    assert core.next(board, "w1") is None


def test_next_claims_the_lowest_rank_and_writes_a_claim_event(board):
    ready(board, "second", rank=20)
    first = ready(board, "first", rank=10)
    claim = core.next(board, "w1")
    assert claim["issue"]["id"] == first
    assert claim["issue"]["state"] == "processing"
    assert claim["issue"]["lease_holder"] == "w1"
    assert claim["lease_token"]
    last = events(board, first)[-1]
    assert (last["kind"], last["actor"], last["from_state"], last["to_state"]) == (
        "claim", "w1", "ready", "processing")


def test_a_live_lease_is_not_handed_out_twice(board):
    ready(board)
    assert core.next(board, "w1") is not None
    assert core.next(board, "w2") is None


def test_onhold_is_not_selectable(board):
    iid = ready(board)
    core.transition(board, iid, "onhold", actor=CHAOS, actor_kind=HUMAN)
    assert core.next(board, "w1") is None


# --- row 1: two parallel claims ----------------------------------------------


def test_row1_parallel_next_has_one_winner(board):
    ready(board)
    barrier = threading.Barrier(2)
    results = {}

    def claim(worker):
        barrier.wait()
        results[worker] = core.next(board, worker)

    threads = [threading.Thread(target=claim, args=(w,)) for w in ("w1", "w2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [w for w, r in results.items() if r is not None]
    assert len(winners) == 1
    assert len(results) == 2


# --- row 2: expiry and reclaim -----------------------------------------------


def test_row2_expired_lease_is_reclaimed_and_names_the_old_holder(board):
    iid = ready(board)
    first = core.next(board, "run-1", ttl=timedelta(seconds=-5))
    core.annotate(board, iid, "tests written", actor="run-1", actor_kind=AGENT,
                  token=first["lease_token"])
    second = core.next(board, "run-2")
    assert second["issue"]["id"] == iid
    assert second["lease_token"] != first["lease_token"]
    kinds = [e["kind"] for e in second["issue"]["events"]]
    assert kinds == ["create", "claim", "annotate", "reclaim"]
    assert "run-1" in second["issue"]["events"][-1]["note"]
    assert "tests written" in [e["note"] for e in second["issue"]["events"]]


def test_the_old_token_is_refused_after_a_reclaim(board):
    iid = ready(board)
    first = core.next(board, "run-1", ttl=timedelta(seconds=-5))
    core.next(board, "run-2")
    with pytest.raises(core.LeaseLost):
        core.transition(board, iid, "done", note="finished", actor="run-1",
                        actor_kind=AGENT, token=first["lease_token"])


def test_heartbeat_extends_the_lease_without_an_event(board):
    iid = ready(board)
    claim = core.next(board, "w1", ttl=timedelta(seconds=-5))
    before = len(events(board, iid))
    core.heartbeat(board, iid, "w1", claim["lease_token"])
    assert len(events(board, iid)) == before
    assert core.next(board, "w2") is None


def test_heartbeat_with_a_stale_token_is_lease_lost(board):
    iid = ready(board)
    core.next(board, "w1")
    with pytest.raises(core.LeaseLost):
        core.heartbeat(board, iid, "w1", "not-the-token")


# --- transitions --------------------------------------------------------------


def test_done_requires_a_note_and_clears_the_lease(board):
    iid = ready(board)
    claim = core.next(board, "w1")
    with pytest.raises(core.NoteRequired):
        core.transition(board, iid, "done", actor="w1", actor_kind=AGENT,
                        token=claim["lease_token"])
    issue = core.transition(board, iid, "done", note="shipped", actor="w1",
                            actor_kind=AGENT, token=claim["lease_token"])
    assert issue["state"] == "done"
    assert issue["lease_holder"] is None
    assert issue["events"][-1]["note"] == "shipped"


def test_an_undeclared_transition_is_refused(board):
    iid = core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN)["id"]
    with pytest.raises(core.InvalidTransition):
        core.transition(board, iid, "done", note="n", actor=CHAOS, actor_kind=HUMAN)


def test_a_lifted_hold_returns_to_backlog_not_ready(board):
    iid = ready(board)
    core.transition(board, iid, "onhold", actor=CHAOS, actor_kind=HUMAN)
    with pytest.raises(core.InvalidTransition):
        core.transition(board, iid, "ready", actor=CHAOS, actor_kind=HUMAN)
    core.transition(board, iid, "backlog", actor=CHAOS, actor_kind=HUMAN)


def test_an_agent_write_needs_the_token(board):
    iid = ready(board)
    core.next(board, "w1")
    with pytest.raises(core.LeaseLost):
        core.annotate(board, iid, "hi", actor="w2", actor_kind=AGENT)


# --- row 3: workflows ----------------------------------------------------------


def test_row3_workflow_steps_are_handed_out_in_order(board):
    core.create_template(board, "release", "Release", ["build", "test", "ship"])
    wf = core.instantiate(board, "release", "MS", actor=CHAOS, actor_kind=HUMAN)
    step1, step2, _ = [s["id"] for s in wf["steps"]]
    claim = core.next(board, "w1")
    assert claim["issue"]["id"] == step1
    assert core.next(board, "w2") is None
    core.transition(board, step1, "done", note="built", actor="w1",
                    actor_kind=AGENT, token=claim["lease_token"])
    assert core.next(board, "w2")["issue"]["id"] == step2


def test_instantiate_is_one_transaction(board):
    with pytest.raises(core.NotFound):
        core.instantiate(board, "no-such-template", "MS", actor=CHAOS, actor_kind=HUMAN)
    with store.session(board) as s:
        assert s.scalars(select(store.Workflow)).all() == []


def test_workflow_state_is_computed(board):
    core.create_template(board, "pair", "Pair", ["a", "b"])
    wf = core.instantiate(board, "pair", "MS", actor=CHAOS, actor_kind=HUMAN)
    a, b = [s["id"] for s in wf["steps"]]
    claim = core.next(board, "w1")
    core.transition(board, a, "need-input", note="stuck", actor="w1", actor_kind=AGENT,
                    token=claim["lease_token"])
    assert core.show(board, b)["workflow"]["state"] == "need-input"


# --- row 4: a closing artifact ------------------------------------------------


def test_row4_an_issue_with_a_closing_artifact_is_never_returned(board):
    iid = ready(board)
    core.link(board, iid, "abc123", kind="commit", closes=True, actor=CHAOS,
              actor_kind=HUMAN)
    assert core.next(board, "w1") is None
    assert core.show(board, iid)["closed_by_artifact"] is True


def test_a_non_closing_artifact_does_not_hide_the_issue(board):
    iid = ready(board)
    core.link(board, iid, "notes.md", kind="path", actor=CHAOS, actor_kind=HUMAN)
    assert core.next(board, "w1")["issue"]["id"] == iid


# --- row 5 and section 8: annotations, idempotency ---------------------------


def test_row5_two_different_annotations_both_survive(board):
    iid = ready(board)
    claim = core.next(board, "w1")
    for note in ("one", "two"):
        core.annotate(board, iid, note, actor="w1", actor_kind=AGENT,
                      token=claim["lease_token"])
    notes = [e["note"] for e in events(board, iid) if e["kind"] == "annotate"]
    assert notes == ["one", "two"]


def test_an_exact_retry_under_one_lease_writes_one_event(board):
    iid = ready(board)
    claim = core.next(board, "w1")
    for _ in range(2):
        core.annotate(board, iid, "same", actor="w1", actor_kind=AGENT,
                      token=claim["lease_token"])
    assert [e["note"] for e in events(board, iid)].count("same") == 1


def test_a_request_id_replays_the_first_result(board):
    first = core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, request_id="r1")
    again = core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, request_id="r1")
    assert first["id"] == again["id"] == "MS-1"
    assert core.create(board, "MS", "y", actor=CHAOS, actor_kind=HUMAN)["id"] == "MS-2"


def test_a_repeated_transition_is_a_no_op_that_still_records(board):
    iid = ready(board)
    claim = core.next(board, "w1")
    tok = claim["lease_token"]
    core.transition(board, iid, "need-input", note="a", actor="w1", actor_kind=AGENT, token=tok)
    core.transition(board, iid, "need-input", note="b", actor=CHAOS, actor_kind=HUMAN)
    last = events(board, iid)[-1]
    assert (last["from_state"], last["to_state"], last["note"]) == ("need-input", "need-input", "b")


# --- row 8: a human edit under a live lease ------------------------------------


def test_row8_edit_under_a_live_lease_needs_preempt(board):
    iid = ready(board)
    claim = core.next(board, "run-2")
    version = core.show(board, iid)["version"]
    with pytest.raises(core.LeaseHeld) as held:
        core.edit(board, iid, actor=CHAOS, expected_version=version, body="new")
    assert held.value.holder == "run-2"
    assert core.show(board, iid)["body"] == ""

    issue = core.edit(board, iid, actor=CHAOS, expected_version=version, body="new",
                      preempt=True)
    assert issue["body"] == "new"
    assert issue["state"] == "processing"
    assert issue["lease_holder"] == CHAOS
    assert issue["lease_expires_at"] is None
    assert [e["kind"] for e in issue["events"]][-2:] == ["preempt", "edit"]
    assert "run-2" in issue["events"][-2]["note"]

    with pytest.raises(core.LeaseLost) as lost:
        core.transition(board, iid, "done", note="old work", actor="run-2",
                        actor_kind=AGENT, token=claim["lease_token"])
    assert CHAOS in str(lost.value)


def test_a_human_held_card_is_never_reclaimed_until_released(board):
    iid = ready(board)
    core.next(board, "run-2")
    version = core.show(board, iid)["version"]
    core.edit(board, iid, actor=CHAOS, expected_version=version, title="t", preempt=True)
    assert core.next(board, "run-3") is None
    core.transition(board, iid, "ready", actor=CHAOS, actor_kind=HUMAN)
    assert core.next(board, "run-3")["issue"]["id"] == iid


def test_a_stale_form_version_is_a_conflict(board):
    iid = ready(board)
    version = core.show(board, iid)["version"]
    core.edit(board, iid, actor=CHAOS, expected_version=version, title="one")
    with pytest.raises(core.Conflict):
        core.edit(board, iid, actor=CHAOS, expected_version=version, title="two")
    assert core.show(board, iid)["title"] == "one"


def test_a_note_needs_no_lease(board):
    iid = ready(board)
    core.next(board, "run-2")
    core.annotate(board, iid, "read the new spec", actor=CHAOS, actor_kind=HUMAN)
    assert events(board, iid)[-1]["note"] == "read the new spec"


def test_every_operation_writes_an_event(board):
    iid = ready(board)
    claim = core.next(board, "w1")
    tok = claim["lease_token"]
    core.annotate(board, iid, "n", actor="w1", actor_kind=AGENT, token=tok)
    core.link(board, iid, "pr/1", kind="pr", actor="w1", actor_kind=AGENT, token=tok)
    core.transition(board, iid, "done", note="d", actor="w1", actor_kind=AGENT, token=tok)
    assert [e["kind"] for e in events(board, iid)] == [
        "create", "claim", "annotate", "link", "transition"]


# --- review fixes: agent leases, keyed preempts, row locks ---------------------


def test_an_agent_moves_an_issue_only_under_a_lease(board):
    iid = ready(board)
    with pytest.raises(core.LeaseLost):
        core.transition(board, iid, "cancelled", actor="rogue", actor_kind=AGENT)
    assert core.show(board, iid)["state"] == "ready"


def test_a_preempt_only_edit_replays_its_request_id(board):
    iid = ready(board, "same")
    core.next(board, "run-2")
    version = core.show(board, iid)["version"]
    first = core.edit(board, iid, actor=CHAOS, expected_version=version, title="same",
                      preempt=True, request_id="take-1")
    again = core.edit(board, iid, actor=CHAOS, expected_version=version, title="same",
                      preempt=True, request_id="take-1")
    assert again["version"] == first["version"]
    assert [e["kind"] for e in again["events"]].count("preempt") == 1


def test_a_stale_token_write_waits_for_a_concurrent_reclaim(board):
    """The write path must lock the row, or READ COMMITTED lets the stale write win."""
    if board.dialect.name != "postgresql":
        pytest.skip("BEGIN IMMEDIATE serialises SQLite writers; this race is PostgreSQL-only")
    iid = ready(board)
    first = core.next(board, "run-1", ttl=timedelta(seconds=-5))
    outcome = {}

    def stale_write():
        try:
            core.transition(board, iid, "done", note="old", actor="run-1",
                            actor_kind=AGENT, token=first["lease_token"])
            outcome["result"] = "written"
        except core.LeaseLost:
            outcome["result"] = "lease lost"

    with board.connect() as conn:
        tx = conn.begin()
        conn.execute(select(store.Issue).where(store.Issue.id == iid).with_for_update())
        conn.execute(store.Issue.__table__.update()
                     .where(store.Issue.id == iid)
                     .values(lease_holder="run-2", lease_token="t2",
                             version=store.Issue.version + 1))
        t = threading.Thread(target=stale_write)
        t.start()
        t.join(0.5)
        tx.commit()
    t.join(10)
    assert outcome == {"result": "lease lost"}
    assert core.show(board, iid)["lease_holder"] == "run-2"


def test_a_reused_request_id_on_another_call_is_not_a_replay(board):
    one, two = ready(board, "one"), ready(board, "two")
    core.annotate(board, one, "n", actor=CHAOS, actor_kind=HUMAN, request_id="run-42")
    core.transition(board, one, "onhold", actor=CHAOS, actor_kind=HUMAN, request_id="run-42")
    core.transition(board, two, "cancelled", actor=CHAOS, actor_kind=HUMAN,
                    request_id="run-42")
    assert core.show(board, one)["state"] == "onhold"
    assert core.show(board, two)["state"] == "cancelled"


def test_an_agent_links_only_under_a_lease(board):
    iid = ready(board)
    with pytest.raises(core.LeaseLost):
        core.link(board, iid, "abc", kind="commit", closes=True, actor="rogue",
                  actor_kind=AGENT)


def test_heartbeat_on_an_unheld_issue_is_lease_lost(board):
    iid = ready(board)
    with pytest.raises(core.LeaseLost):
        core.heartbeat(board, iid, "w1", None)


# --- review 3: a request id names one call ----------------------------------------


def test_a_reused_request_id_with_other_arguments_is_a_conflict(board):
    iid = ready(board)
    core.annotate(board, iid, "checkpoint one", actor=CHAOS, actor_kind=HUMAN,
                  request_id="run-42")
    with pytest.raises(core.Conflict):
        core.annotate(board, iid, "checkpoint two", actor=CHAOS, actor_kind=HUMAN,
                      request_id="run-42")
    notes = [e["note"] for e in events(board, iid)]
    assert "checkpoint one" in notes and "checkpoint two" not in notes


def test_a_reused_create_request_id_with_another_title_is_a_conflict(board):
    core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, request_id="r1")
    with pytest.raises(core.Conflict):
        core.create(board, "MS", "y", actor=CHAOS, actor_kind=HUMAN, request_id="r1")


def test_an_exact_retry_with_a_request_id_still_replays(board):
    iid = ready(board)
    first = core.annotate(board, iid, "n", actor=CHAOS, actor_kind=HUMAN, request_id="r")
    again = core.annotate(board, iid, "n", actor=CHAOS, actor_kind=HUMAN, request_id="r")
    assert again["version"] == first["version"]
    assert [e["note"] for e in events(board, iid)].count("n") == 1


def test_next_request_ids_are_per_worker(board):
    one, two = ready(board, "one"), ready(board, "two")
    assert core.next(board, "w1", request_id="run-42")["issue"]["id"] == one
    assert core.next(board, "w2", request_id="run-42")["issue"]["id"] == two


def test_an_instantiate_request_id_reused_for_another_project_is_a_conflict(board):
    core.create_project(board, "BR", "brain")
    core.create_template(board, "rel", "release", ["build", "ship"])
    core.instantiate(board, "rel", "MS", actor=CHAOS, actor_kind=HUMAN, request_id="i1")
    with pytest.raises(core.Conflict):
        core.instantiate(board, "rel", "BR", actor=CHAOS, actor_kind=HUMAN,
                         request_id="i1")


def test_an_agent_cannot_edit(board):
    core.create(board, "MS", "open item", actor=CHAOS, actor_kind=HUMAN)
    version = core.show(board, "MS-1")["version"]
    with pytest.raises(core.BoardError):
        core.edit(board, "MS-1", actor="rogue", actor_kind=AGENT,
                  expected_version=version, state="ready", title="rewritten")
    issue = core.show(board, "MS-1")
    assert (issue["state"], issue["title"]) == ("backlog", "open item")


def test_a_concurrent_retry_replays_after_the_first_commits(board):
    """The retry blocks on the row lock, then must replay rather than see LeaseLost."""
    if board.dialect.name != "postgresql":
        pytest.skip("BEGIN IMMEDIATE serialises SQLite writers; this race is PostgreSQL-only")
    iid = ready(board)
    tok = core.next(board, "w1")["lease_token"]
    outcome = {}

    def retry():
        try:
            outcome["state"] = core.transition(
                board, iid, "done", note="d", actor="w1", actor_kind=AGENT, token=tok,
                request_id="fin")["state"]
        except core.BoardError as e:
            outcome["state"] = type(e).__name__

    with board.connect() as conn:
        tx = conn.begin()
        conn.execute(select(store.Issue).where(store.Issue.id == iid).with_for_update())
        t = threading.Thread(target=retry)
        t.start()
        t.join(0.5)
        tx.rollback()
    core.transition(board, iid, "done", note="d", actor="w1", actor_kind=AGENT,
                    token=tok, request_id="fin")
    t.join(10)
    assert outcome == {"state": "done"}


# --- overview: the read the web board renders from ------------------------------


def test_overview_lists_every_issue_in_rank_order_with_the_db_clock(board):
    b = ready(board, "second", rank=20)
    a = ready(board, "first", rank=10)
    core.create_template(board, "rel", "Release", ["build", "ship"])
    wf = core.instantiate(board, "rel", "MS", actor=CHAOS, actor_kind=HUMAN)
    core.next(board, "run-1")
    view = core.overview(board)
    assert view["now"] is not None
    ids = [i["id"] for i in view["issues"]]
    assert ids.index(a) < ids.index(b)
    held = next(i for i in view["issues"] if i["lease_holder"] == "run-1")
    assert held["lease_expires_at"] is not None and "lease_token" not in held
    step = next(i for i in view["issues"] if i["id"] == wf["steps"][1]["id"])
    assert (step["workflow_id"], step["position"]) == (wf["id"], 2)
    assert [w["id"] for w in view["workflows"]] == [wf["id"]]
    assert [p["key"] for p in view["projects"]] == ["MS"]
    assert [t["name"] for t in view["templates"]] == ["rel"]


def test_a_second_project_with_a_taken_key_is_a_conflict(board):
    with pytest.raises(core.Conflict):
        core.create_project(board, "MS", "again")
    assert [p["key"] for p in core.overview(board)["projects"]] == ["MS"]


# --- managing projects (MS-642) ----------------------------------------------


def test_projects_lists_each_with_its_issue_count(board):
    core.create_project(board, "BR", "brain")
    ready(board)
    ready(board)
    assert core.projects(board) == [{"key": "BR", "name": "brain", "issues": 0},
                                    {"key": "MS", "name": "memory-solution", "issues": 2}]


def test_rename_project_changes_the_name_and_keeps_the_key_and_ids(board):
    iid = ready(board)
    assert core.rename_project(board, "MS", "  memory ") == {"key": "MS", "name": "memory"}
    assert core.projects(board) == [{"key": "MS", "name": "memory", "issues": 1}]
    assert core.show(board, iid)["project"] == "MS"


def test_rename_project_refuses_a_blank_name_and_an_unknown_key(board):
    with pytest.raises(core.BoardError):
        core.rename_project(board, "MS", "   ")
    with pytest.raises(core.NotFound):
        core.rename_project(board, "ZZ", "nope")
    assert core.projects(board)[0]["name"] == "memory-solution"


def test_delete_project_removes_an_empty_project(board):
    core.create_project(board, "BR", "brain")
    core.delete_project(board, "BR")
    assert [p["key"] for p in core.projects(board)] == ["MS"]
    with pytest.raises(core.NotFound):
        core.delete_project(board, "BR")


def test_delete_project_refuses_a_project_with_issues(board):
    ready(board)
    with pytest.raises(core.BoardError, match="1 issue"):
        core.delete_project(board, "MS")
    assert [p["key"] for p in core.projects(board)] == ["MS"]


@pytest.mark.parametrize("key", ["a/b", "..", "ms", "M S", "1AB", "A" * 17])
def test_create_project_refuses_a_key_that_is_not_a_short_uppercase_word(board, key):
    """The key is a path segment in every issue URL, so a `/` would strand the project."""
    with pytest.raises(core.BoardError, match="key"):
        core.create_project(board, key, "x")
    assert [p["key"] for p in core.projects(board)] == ["MS"]


def test_project_names_over_200_characters_are_refused(board):
    with pytest.raises(core.BoardError, match="200"):
        core.create_project(board, "BR", "n" * 201)
    with pytest.raises(core.BoardError, match="200"):
        core.rename_project(board, "MS", "n" * 201)
    core.create_project(board, "A1" + "B" * 14, "n" * 200)


def test_create_project_refuses_a_blank_key_or_name(board):
    for key, name in (("", "x"), ("  ", "x"), ("BR", ""), ("BR", "  ")):
        with pytest.raises(core.BoardError):
            core.create_project(board, key, name)
    assert [p["key"] for p in core.projects(board)] == ["MS"]


# --- search and filters (MS-629) ---------------------------------------------


def ids(results):
    return [i["id"] for i in results]


@pytest.fixture
def searchable(board):
    core.create_project(board, "BR", "brain")
    a = core.create(board, "MS", "Search box", actor=CHAOS, actor_kind=HUMAN,
                    body="filter the board", labels=["web"])["id"]
    b = core.create(board, "MS", "Lease expiry", actor=CHAOS, actor_kind=HUMAN,
                    body="100% of claims", labels=["core"])["id"]
    c = core.create(board, "BR", "Wrap-up", actor=CHAOS, actor_kind=HUMAN,
                    labels=["web", "prompt"])["id"]
    with store.session(board) as s, s.begin():
        s.get(store.Issue, b).assignee = "worker-1"
    return board, a, b, c


def test_search_with_no_filters_returns_every_issue(searchable):
    board, a, b, c = searchable
    assert sorted(ids(core.search(board))) == sorted([a, b, c])


@pytest.mark.parametrize("q, which", [
    ("SEARCH", "a"),        # title, case-insensitive
    ("the board", "a"),     # body
    ("br-", "c"),           # id
    ("prompt", "c"),        # label
    ("nothing matches", None),
])
def test_search_q_matches_id_title_body_and_labels(searchable, q, which):
    board, a, b, c = searchable
    expected = {"a": [a], "c": [c], None: []}[which]
    assert ids(core.search(board, q=q)) == expected


def test_search_q_treats_like_wildcards_as_literals(searchable):
    board, a, b, c = searchable
    assert ids(core.search(board, q="100%")) == [b]
    assert ids(core.search(board, q="%")) == [b]
    assert ids(core.search(board, q="_")) == []


def test_search_filters_by_project_label_and_assignee(searchable):
    board, a, b, c = searchable
    assert ids(core.search(board, project="BR")) == [c]
    assert sorted(ids(core.search(board, label="web"))) == sorted([a, c])
    assert ids(core.search(board, assignee="worker-1")) == [b]


def test_search_filters_combine_with_q(searchable):
    board, a, b, c = searchable
    assert ids(core.search(board, q="w", label="web", project="MS")) == [a]
    assert ids(core.search(board, q="lease", label="web")) == []


def test_a_label_match_returns_the_issue_once_with_all_its_labels(searchable):
    board, a, b, c = searchable
    [hit] = core.search(board, label="web", project="BR")
    assert hit["labels"] == ["prompt", "web"]


def test_overview_filters_its_issues_and_lists_the_filter_choices(searchable):
    board, a, b, c = searchable
    view = core.overview(board, label="core")
    assert ids(view["issues"]) == [b]
    assert view["labels"] == ["core", "prompt", "web"]
    assert view["assignees"] == ["worker-1"]
    assert [p["key"] for p in view["projects"]] == ["BR", "MS"]


def test_the_assignee_filter_matches_a_lease_holder(board):
    """Nothing writes `assignee` yet, so the card's holder counts as its assignee."""
    held = ready(board, "claimed")
    ready(board, "queued")
    assert core.next(board, "worker-2")["issue"]["id"] == held
    assert ids(core.search(board, assignee="worker-2")) == [held]
    assert core.overview(board)["assignees"] == ["worker-2"]


def test_search_q_is_trimmed_and_folds_case_the_same_on_both_sides(searchable):
    board, a, b, c = searchable
    e = core.create(board, "MS", "Épée résumé", actor=CHAOS, actor_kind=HUMAN)["id"]
    assert ids(core.search(board, q="  search  ")) == [a]
    assert ids(core.search(board, q="Épée")) == [e]
    assert ids(core.search(board, q="c:\\temp")) == []


def test_workflow_lists_its_steps_in_position_order_with_leases(board):
    core.create_template(board, "rel", "Release", ["build", "ship"])
    wf = core.instantiate(board, "rel", "MS", actor=CHAOS, actor_kind=HUMAN)
    core.next(board, "w1")
    view = core.workflow(board, wf["id"])
    assert view["title"] == "Release" and view["state"] == "processing"
    assert [st["id"] for st in view["steps"]] == [st["id"] for st in wf["steps"]]
    assert [st["position"] for st in view["steps"]] == sorted(st["position"] for st in view["steps"])
    assert view["steps"][0]["lease_holder"] == "w1"
    assert view["steps"][0]["lease_expires_at"] is not None
    assert view["steps"][1]["lease_holder"] is None
    assert view["current"] == wf["steps"][0]["id"]
    assert view["now"]


def test_an_unknown_workflow_is_not_found(board):
    with pytest.raises(core.NotFound):
        core.workflow(board, 999)


# --- lanes (MS-632) -------------------------------------------------------------


def test_the_lanes_are_the_seven_in_board_order():
    assert list(core.TRANSITIONS) == ["backlog", "ready", "need-input", "processing",
                                      "onhold", "done", "cancelled"]


def test_every_lane_has_a_one_line_hint_in_board_order():
    """MS-660: a new state cannot ship without a hint for its lane."""
    assert list(core.LANE_HINTS) == list(core.TRANSITIONS)
    for state, hint in core.LANE_HINTS.items():
        assert hint.strip() and "\n" not in hint, state


def test_a_new_issue_lands_in_backlog(board):
    assert core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN)["state"] == "backlog"


@pytest.mark.parametrize("old", ["open", "blocked", "in-progress", "frozen"])
def test_a_retired_state_name_is_refused(board, old):
    iid = core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN)["id"]
    with pytest.raises(core.InvalidTransition, match="unknown state"):
        core.transition(board, iid, old, note="n", actor=CHAOS, actor_kind=HUMAN)
    with pytest.raises(core.InvalidTransition):
        core.create(board, "MS", "y", actor=CHAOS, actor_kind=HUMAN, state=old)


def test_a_workflow_of_ready_steps_sits_in_ready(board):
    core.create_template(board, "pair", "Pair", ["a", "b"])
    wf = core.instantiate(board, "pair", "MS", actor=CHAOS, actor_kind=HUMAN)
    assert wf["state"] == "ready"
    core.transition(board, wf["steps"][0]["id"], "onhold", actor=CHAOS, actor_kind=HUMAN)
    core.transition(board, wf["steps"][1]["id"], "onhold", actor=CHAOS, actor_kind=HUMAN)
    assert core.show(board, wf["steps"][0]["id"])["workflow"]["state"] == "backlog"


def test_a_workflow_whose_first_open_step_is_held_is_not_ready(board):
    core.create_template(board, "pair", "Pair", ["a", "b"])
    wf = core.instantiate(board, "pair", "MS", actor=CHAOS, actor_kind=HUMAN)
    core.transition(board, wf["steps"][0]["id"], "onhold", actor=CHAOS, actor_kind=HUMAN)
    assert core.next(board, "w1") is None
    assert core.show(board, wf["steps"][1]["id"])["workflow"]["state"] == "backlog"


# --- attachments (MS-643) -------------------------------------------------------


def blob(name="a.txt", content_type="text/plain", data=b"hello"):
    import hashlib
    return {"filename": name, "content_type": content_type, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def test_create_records_attachments_on_the_issue(board):
    issue = core.create(board, "MS", "with a file", actor=CHAOS, actor_kind=HUMAN,
                        attachments=[blob()])
    [a] = issue["attachments"]
    assert (a["filename"], a["content_type"], a["size"], a["event_id"], a["added_by"]) == (
        "a.txt", "text/plain", 5, None, CHAOS)
    assert core.attachment(board, a["id"])["sha256"] == blob()["sha256"]


def test_a_notes_files_hang_off_that_note(board):
    iid = ready(board)
    issue = core.annotate(board, iid, "see file", actor=CHAOS, actor_kind=HUMAN,
                          attachments=[blob("log.txt")])
    note = issue["events"][-1]
    assert note["kind"] == "annotate"
    assert [a["filename"] for a in note["attachments"]] == ["log.txt"]
    assert issue["attachments"][0]["event_id"] == note["id"]
    assert issue["events"][0]["attachments"] == []


def test_an_edit_that_only_attaches_is_a_change(board):
    iid = ready(board)
    before = core.show(board, iid)
    after = core.edit(board, iid, actor=CHAOS, expected_version=before["version"],
                      attachments=[blob()])
    assert after["version"] == before["version"] + 1
    assert after["events"][-1]["note"] == "changed attachments"
    assert [a["event_id"] for a in after["attachments"]] == [None]


def test_an_edit_that_moves_state_attaches_to_the_transition(board):
    iid = ready(board)
    before = core.show(board, iid)
    after = core.edit(board, iid, actor=CHAOS, expected_version=before["version"],
                      state="onhold", note="parked", attachments=[blob()])
    assert after["version"] == before["version"] + 1
    last = after["events"][-1]
    assert (last["kind"], last["to_state"]) == ("transition", "onhold")
    assert [a["filename"] for a in last["attachments"]] == ["a.txt"]
    assert [e["kind"] for e in after["events"]].count("edit") == 0


def test_an_unknown_attachment_is_not_found(board):
    with pytest.raises(core.NotFound):
        core.attachment(board, 999)


@pytest.mark.parametrize("bad", ["../../etc/passwd", "ABC", "a" * 63, "g" * 64, "A" * 64])
def test_an_attachment_digest_must_be_a_lowercase_sha256(board, bad):
    """The digest names the file on disk, so anything else could point outside the store."""
    with pytest.raises(core.BoardError):
        core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN,
                    attachments=[blob() | {"sha256": bad}])
    assert core.overview(board)["issues"] == []


def test_a_fileless_retry_still_replays_a_request_hashed_before_attachments(board):
    """Request hashes written before MS-643 carry no file list, and a fileless call must match them."""
    import hashlib
    import json
    iid = ready(board)
    core.annotate(board, iid, "n", actor=CHAOS, actor_kind=HUMAN, request_id="r")
    old = hashlib.sha256(json.dumps(["n", CHAOS, HUMAN, None]).encode()).hexdigest()
    with store.session(board) as s, s.begin():
        ev = s.scalar(select(store.Event).where(store.Event.idempotency_key.like("%:r")))
        assert ev.request_hash == old
    again = core.annotate(board, iid, "n", actor=CHAOS, actor_kind=HUMAN, request_id="r")
    assert len(again["events"]) == len(core.show(board, iid)["events"])


# --- MS-644: an agent plans an issue as a workflow ------------------------------


def claimed(engine, title="big job"):
    iid = ready(engine, title)
    claim = core.next(engine, "w1")
    assert claim["issue"]["id"] == iid
    return iid, claim["lease_token"]


def plan(engine, iid, token, steps=("design", "build", "test"), **kw):
    return core.plan(engine, iid, [{"title": t} for t in steps], actor="w1",
                     actor_kind=AGENT, token=token, **kw)


def test_plan_creates_ordered_ready_steps_and_holds_the_origin(board):
    iid, token = claimed(board)
    view = plan(board, iid, token)
    assert view["id"] == iid
    assert view["state"] == "onhold"
    assert view["lease_holder"] is None
    wf = view["plan"]
    assert wf["title"] == "big job"
    assert [(s["position"], s["title"], s["state"]) for s in wf["steps"]] == [
        (1, "design", "ready"), (2, "build", "ready"), (3, "test", "ready")]
    step = core.show(board, wf["steps"][0]["id"])
    assert step["project"] == "MS"
    # The steps keep the origin's place in the queue rather than going to the back.
    assert {core.show(board, s["id"])["rank"] for s in wf["steps"]} == {view["rank"]}
    assert step["workflow"]["origin_issue_id"] == iid
    last = events(board, iid)[-1]
    assert (last["from_state"], last["to_state"], last["actor"]) == ("processing", "onhold", "w1")
    assert "3 steps" in last["note"]


def test_plan_step_takes_its_own_body_and_project(board):
    core.create_project(board, "OPS", "operations")
    iid, token = claimed(board)
    view = core.plan(board, iid, [{"title": "a", "body": "do a", "project": "OPS"},
                                  {"title": "b"}],
                     actor="w1", actor_kind=AGENT, token=token)
    a, b = [core.show(board, s["id"]) for s in view["plan"]["steps"]]
    assert (a["project"], a["body"], b["project"]) == ("OPS", "do a", "MS")


def test_plan_needs_the_callers_live_lease(board):
    iid = ready(board)
    with pytest.raises(core.LeaseLost):
        core.plan(board, iid, [{"title": "a"}], actor="w1", actor_kind=AGENT, token=None)
    claim = core.next(board, "w1")
    with pytest.raises(core.LeaseLost):
        core.plan(board, iid, [{"title": "a"}], actor="w1", actor_kind=AGENT, token="stale")
    with pytest.raises(core.LeaseLost):
        core.plan(board, iid, [{"title": "a"}], actor="w2", actor_kind=AGENT,
                  token=claim["lease_token"])
    assert core.show(board, iid)["plan"] is None


def test_plan_without_steps_is_refused_and_writes_nothing(board):
    iid, token = claimed(board)
    with pytest.raises(core.BoardError):
        plan(board, iid, token, steps=())
    with pytest.raises(core.BoardError):
        plan(board, iid, token, steps=("ok", "  "))
    with store.session(board) as s:
        assert s.scalars(select(store.Workflow)).all() == []
    assert core.show(board, iid)["state"] == "processing"


def test_a_second_plan_on_one_issue_is_refused(board):
    iid, token = claimed(board)
    plan(board, iid, token)
    with pytest.raises(core.Conflict):
        core.plan(board, iid, [{"title": "again"}], actor=CHAOS, actor_kind=HUMAN)


def test_plan_replays_one_request_id_without_duplicates(board):
    iid, token = claimed(board)
    first = plan(board, iid, token, request_id="r1")
    again = plan(board, iid, token, request_id="r1")
    assert again["plan"]["id"] == first["plan"]["id"]
    with store.session(board) as s:
        assert len(s.scalars(select(store.Workflow)).all()) == 1
        assert len(s.scalars(select(store.Issue)).all()) == 4
    with pytest.raises(core.Conflict):
        plan(board, iid, token, steps=("other",), request_id="r1")


def test_a_human_plans_a_backlog_issue_without_a_lease(board):
    iid = core.create(board, "MS", "idea", actor=CHAOS, actor_kind=HUMAN)["id"]
    view = core.plan(board, iid, [{"title": "a"}], actor=CHAOS, actor_kind=HUMAN)
    assert view["state"] == "onhold"
    assert len(view["plan"]["steps"]) == 1


def test_a_human_plan_under_an_agents_lease_needs_preempt(board):
    iid, _ = claimed(board)
    with pytest.raises(core.LeaseHeld):
        core.plan(board, iid, [{"title": "a"}], actor=CHAOS, actor_kind=HUMAN)
    view = core.plan(board, iid, [{"title": "a"}], actor=CHAOS, actor_kind=HUMAN,
                     preempt=True)
    assert (view["state"], view["lease_holder"]) == ("onhold", None)


def test_a_finished_issue_cannot_be_planned(board):
    iid = ready(board)
    core.transition(board, iid, "cancelled", actor=CHAOS, actor_kind=HUMAN)
    with pytest.raises(core.InvalidTransition):
        core.plan(board, iid, [{"title": "a"}], actor=CHAOS, actor_kind=HUMAN)


def test_a_step_cannot_be_planned_into_a_nested_workflow(board):
    iid, token = claimed(board)
    step = plan(board, iid, token)["plan"]["steps"][0]["id"]
    claim = core.next(board, "w1")
    assert claim["issue"]["id"] == step
    with pytest.raises(core.BoardError):
        core.plan(board, step, [{"title": "x"}], actor="w1", actor_kind=AGENT,
                  token=claim["lease_token"])


def test_the_last_step_done_returns_the_origin_to_ready(board):
    iid, token = claimed(board)
    plan(board, iid, token, steps=("a", "b"))
    for note in ("did a", "did b"):
        assert core.show(board, iid)["state"] == "onhold"
        claim = core.next(board, "w2")
        core.transition(board, claim["issue"]["id"], "done", note=note, actor="w2",
                        actor_kind=AGENT, token=claim["lease_token"])
    origin = core.show(board, iid)
    assert origin["state"] == "ready"
    last = origin["events"][-1]
    assert (last["from_state"], last["to_state"], last["actor_kind"]) == (
        "onhold", "ready", "system")
    assert core.next(board, "w3")["issue"]["id"] == iid


def test_an_origin_moved_off_hold_by_a_human_is_left_alone(board):
    iid, token = claimed(board)
    plan(board, iid, token, steps=("a",))
    core.transition(board, iid, "backlog", actor=CHAOS, actor_kind=HUMAN)
    claim = core.next(board, "w2")
    core.transition(board, claim["issue"]["id"], "done", note="did a", actor="w2",
                    actor_kind=AGENT, token=claim["lease_token"])
    assert core.show(board, iid)["state"] == "backlog"


# --- MS-646: dependencies between issues ------------------------------------------


def depend(engine, iid, on, **kw):
    return core.depend(engine, iid, on, actor=CHAOS, actor_kind=HUMAN, **kw)


def finish(engine, claim, note="did it"):
    core.transition(engine, claim["issue"]["id"], "done", note=note,
                    actor=claim["issue"]["lease_holder"], actor_kind=AGENT,
                    token=claim["lease_token"])


def test_next_skips_an_issue_until_what_it_depends_on_is_done(board):
    blocked = ready(board, "blocked", rank=10)
    first = ready(board, "first", rank=20)
    depend(board, blocked, first)
    claim = core.next(board, "w1")
    assert claim["issue"]["id"] == first
    assert core.next(board, "w2") is None
    finish(board, claim)
    assert core.next(board, "w2")["issue"]["id"] == blocked


def test_depend_is_listed_both_ways_and_logged(board):
    a, b = ready(board, "a"), ready(board, "b")
    view = depend(board, b, a)
    assert [d["id"] for d in view["depends_on"]] == [a]
    assert view["depends_on"][0]["state"] == "ready"
    assert [d["id"] for d in core.show(board, a)["blocks"]] == [b]
    ev = events(board, b)[-1]
    assert (ev["kind"], ev["actor"]) == ("depend", CHAOS)
    assert a in ev["note"]


def test_undepend_removes_the_edge_and_logs_it(board):
    a, b = ready(board, "a", rank=20), ready(board, "b", rank=10)
    depend(board, b, a)
    view = core.undepend(board, b, a, actor=CHAOS, actor_kind=HUMAN)
    assert view["depends_on"] == []
    assert events(board, b)[-1]["kind"] == "undepend"
    assert core.next(board, "w1")["issue"]["id"] == b


def test_undepend_of_a_missing_edge_is_not_found(board):
    a, b = ready(board, "a"), ready(board, "b")
    with pytest.raises(core.NotFound):
        core.undepend(board, b, a, actor=CHAOS, actor_kind=HUMAN)


def test_depend_twice_is_a_no_op(board):
    a, b = ready(board, "a"), ready(board, "b")
    depend(board, b, a)
    view = depend(board, b, a)
    assert [d["id"] for d in view["depends_on"]] == [a]
    assert [e["kind"] for e in events(board, b)].count("depend") == 1


def test_a_self_dependency_is_refused(board):
    a = ready(board, "a")
    with pytest.raises(core.BoardError):
        depend(board, a, a)
    assert core.show(board, a)["depends_on"] == []


def test_an_edge_that_closes_a_cycle_is_refused_and_writes_nothing(board):
    a, b, c = ready(board, "a"), ready(board, "b"), ready(board, "c")
    depend(board, b, a)
    depend(board, c, b)
    with pytest.raises(core.BoardError, match="cycle"):
        depend(board, a, c)
    assert core.show(board, a)["depends_on"] == []
    assert "depend" not in [e["kind"] for e in events(board, a)]


def test_depend_on_an_unknown_issue_is_not_found(board):
    a = ready(board, "a")
    with pytest.raises(core.NotFound):
        depend(board, a, "MS-999")
    with pytest.raises(core.NotFound):
        depend(board, "MS-999", a)


def test_a_workflow_with_no_declared_dependencies_stays_strict(board):
    core.create_template(board, "release", "Release", ["build", "test", "ship"])
    wf = core.instantiate(board, "release", "MS", actor=CHAOS, actor_kind=HUMAN)
    loose_a, loose_b = ready(board, "loose a"), ready(board, "loose b")
    depend(board, loose_b, loose_a)
    first = core.next(board, "w1")
    assert first["issue"]["id"] == wf["steps"][0]["id"]
    # The strict workflow holds its step 2 back; the loose one is next.
    assert core.next(board, "w2")["issue"]["id"] == loose_a


def test_once_a_step_declares_dependencies_steps_run_in_parallel(board):
    core.create_template(board, "fan", "Fan", ["design", "left", "right", "join"])
    wf = core.instantiate(board, "fan", "MS", actor=CHAOS, actor_kind=HUMAN)
    design, left, right, join = [s["id"] for s in wf["steps"]]
    depend(board, left, design)
    depend(board, right, design)
    depend(board, join, left)
    depend(board, join, right)
    claim = core.next(board, "w1")
    assert claim["issue"]["id"] == design
    assert core.next(board, "w2") is None
    finish(board, claim)
    a, b = core.next(board, "w2"), core.next(board, "w3")
    assert {a["issue"]["id"], b["issue"]["id"]} == {left, right}
    assert core.next(board, "w4") is None
    finish(board, a)
    assert core.next(board, "w4") is None
    finish(board, b)
    assert core.next(board, "w4")["issue"]["id"] == join


def test_a_step_with_no_dependency_in_a_declared_workflow_starts_at_once(board):
    core.create_template(board, "two", "Two", ["first", "second"])
    wf = core.instantiate(board, "two", "MS", actor=CHAOS, actor_kind=HUMAN)
    first, second = [s["id"] for s in wf["steps"]]
    other = ready(board, "outside")
    depend(board, first, other)
    # `second` declares nothing, and the workflow is no longer strict.
    assert core.next(board, "w1")["issue"]["id"] == second


def test_a_dependency_across_workflows_is_honoured(board):
    core.create_template(board, "one", "One", ["only"])
    wf1 = core.instantiate(board, "one", "MS", actor=CHAOS, actor_kind=HUMAN)
    wf2 = core.instantiate(board, "one", "MS", actor=CHAOS, actor_kind=HUMAN)
    a, b = wf1["steps"][0]["id"], wf2["steps"][0]["id"]
    core.edit(board, a, actor=CHAOS, expected_version=1, rank=100)
    depend(board, b, a)
    claim = core.next(board, "w1")
    assert claim["issue"]["id"] == a
    assert core.next(board, "w2") is None
    finish(board, claim)
    assert core.next(board, "w2")["issue"]["id"] == b


def test_a_parallel_workflow_is_done_only_when_every_step_is(board):
    core.create_template(board, "two", "Two", ["first", "second"])
    wf = core.instantiate(board, "two", "MS", actor=CHAOS, actor_kind=HUMAN)
    first, second = [s["id"] for s in wf["steps"]]
    # Never authorized, so `first` stays blocked while `second` finishes.
    other = core.create(board, "MS", "outside", actor=CHAOS, actor_kind=HUMAN)["id"]
    depend(board, first, other)
    claim = core.next(board, "w1")
    assert claim["issue"]["id"] == second
    finish(board, claim)
    assert core.show(board, second)["workflow"]["state"] != "done"


def test_depend_replays_on_the_same_request_id(board):
    a, b = ready(board, "a"), ready(board, "b")
    depend(board, b, a, request_id="r1")
    depend(board, b, a, request_id="r1")
    assert [e["kind"] for e in events(board, b)].count("depend") == 1


def test_a_workflows_steps_list_what_each_depends_on(board):
    core.create_template(board, "fan", "Fan", ["design", "left", "right"])
    wf = core.instantiate(board, "fan", "MS", actor=CHAOS, actor_kind=HUMAN)
    design, left, right = [s["id"] for s in wf["steps"]]
    other = ready(board, "outside")
    depend(board, left, design)
    depend(board, right, design)
    depend(board, right, other)
    steps = core.show(board, design)["workflow"]["steps"]
    assert [st["depends_on"] for st in steps] == [[], [design], sorted([design, other])]


def test_a_concurrent_depend_cannot_close_a_cycle(board):
    """Each side of a cycle locks a different row, so the graph needs one lock of its own."""
    if board.dialect.name != "postgresql":
        pytest.skip("BEGIN IMMEDIATE serialises SQLite writers; this race is PostgreSQL-only")
    a, b = ready(board, "a"), ready(board, "b")
    outcome = {}

    def other_side():
        try:
            depend(board, a, b)
            outcome["result"] = "written"
        except core.BoardError as e:
            outcome["result"] = str(e)

    # A concurrent `depend(b, on=a)`, holding the dependency lock, not yet committed.
    with board.connect() as conn:
        tx = conn.begin()
        core._lock_dependencies(conn)
        conn.execute(store.Dependency.__table__.insert().values(
            issue_id=b, depends_on_id=a, created_at=func.current_timestamp(), created_by=CHAOS))
        t = threading.Thread(target=other_side)
        t.start()
        t.join(0.5)
        tx.commit()
    t.join(10)
    assert "cycle" in outcome["result"]
    assert core.show(board, a)["depends_on"] == []


def test_parallel_last_steps_finishing_together_still_resume_the_origin(board):
    """Each finisher sees the other step open unless the origin lock orders them."""
    if board.dialect.name != "postgresql":
        pytest.skip("BEGIN IMMEDIATE serialises SQLite writers; this race is PostgreSQL-only")
    iid, token = claimed(board)
    view = plan(board, iid, token, steps=("left", "right"))
    left, right = [st["id"] for st in view["plan"]["steps"]]
    never = core.create(board, "MS", "never", actor=CHAOS, actor_kind=HUMAN)["id"]
    depend(board, left, never)
    claim = core.next(board, "w2")
    assert claim["issue"]["id"] == right
    outcome = {}

    def finish_right():
        finish(board, claim)
        outcome["done"] = True

    # `left` finishing in a concurrent transaction that has reached the origin lock.
    with board.connect() as conn:
        tx = conn.begin()
        conn.execute(select(store.Issue).where(store.Issue.id == iid).with_for_update())
        conn.execute(store.Issue.__table__.update().where(store.Issue.id == left)
                     .values(state="done"))
        t = threading.Thread(target=finish_right)
        t.start()
        t.join(0.5)
        tx.commit()
    t.join(10)
    assert outcome == {"done": True}
    assert core.show(board, iid)["state"] == "ready"


# --- MS-647: create a batch of issues in one call -----------------------------


def batch(engine, items, **kw):
    return core.create_batch(engine, items, actor=CHAOS, actor_kind=HUMAN, **kw)


def issue_count(engine):
    with store.session(engine) as s:
        return s.scalar(select(func.count()).select_from(store.Issue))


def test_batch_creates_ready_issues_in_list_order(board):
    out = batch(board, [{"project": "MS", "title": "a"}, {"project": "MS", "title": "b",
                                                            "body": "why", "labels": ["x"]}])
    a, b = out["ids"]
    assert out["workflow_id"] is None
    assert core.show(board, a)["state"] == "ready"
    view = core.show(board, b)
    assert (view["title"], view["body"], view["labels"]) == ("b", "why", ["x"])
    assert core.next(board, "w1")["issue"]["id"] == a


def test_batch_is_all_or_nothing_when_the_last_item_is_bad(board):
    items = [{"project": "MS", "title": "a"}, {"project": "MS", "title": "b"},
             {"project": "NOPE", "title": "c"}]
    with pytest.raises(core.BoardError):
        batch(board, items)
    assert issue_count(board) == 0


@pytest.mark.parametrize("bad", [
    {"project": "MS", "title": "  "},
    {"project": "MS", "title": "c", "after": [7]},
    {"project": "MS", "title": "c", "after": ["no-such-ref"]},
    {"project": "MS", "title": "c", "after": [2]},
    {"project": "MS", "title": "c", "state": "done"},
    {"project": "MS", "title": "c", "ref": "a"},
    {"project": "MS", "title": "c", "rank": "high"},
    {"project": "MS", "title": "c", "rank": True},
    {"project": "MS", "title": "c", "labels": "bug"},
    {"project": "MS", "title": "c", "labels": [3]},
    {"project": "MS", "title": "c", "labels": ["  "]},
    {"project": ["MS"], "title": "c"},
    "not an item",
])
def test_batch_validates_every_item_before_writing(board, bad):
    items = [{"project": "MS", "title": "a", "ref": "a"}, {"project": "MS", "title": "b"}, bad]
    with pytest.raises(core.BoardError):
        batch(board, items)
    assert issue_count(board) == 0


def test_batch_refuses_a_cycle_among_its_items(board):
    items = [{"project": "MS", "title": "a", "ref": "a", "after": ["b"]},
             {"project": "MS", "title": "b", "ref": "b", "after": ["a"]}]
    with pytest.raises(core.BoardError, match="cycle"):
        batch(board, items)
    assert issue_count(board) == 0


def test_batch_after_by_index_by_ref_and_by_existing_id(board):
    old = ready(board, "old")
    out = batch(board, [{"project": "MS", "title": "design", "ref": "d"},
                        {"project": "MS", "title": "left", "after": [0]},
                        {"project": "MS", "title": "right", "after": ["d", old]}])
    design, left, right = out["ids"]
    assert out["refs"] == {"d": design}
    assert [d["id"] for d in core.show(board, left)["depends_on"]] == [design]
    assert sorted(d["id"] for d in core.show(board, right)["depends_on"]) == sorted([design, old])
    assert "depend" in [e["kind"] for e in events(board, right)]


def test_batch_as_a_workflow_runs_steps_in_parallel(board):
    out = batch(board, [{"project": "MS", "title": "design"},
                        {"project": "MS", "title": "left", "after": [0]},
                        {"project": "MS", "title": "right", "after": [0]}],
                workflow_title="Fan out")
    design, left, right = out["ids"]
    wf = core.workflow(board, out["workflow_id"])
    assert wf["title"] == "Fan out"
    assert [st["id"] for st in wf["steps"]] == [design, left, right]
    finish(board, core.next(board, "w1"))
    got = {core.next(board, "w2")["issue"]["id"], core.next(board, "w3")["issue"]["id"]}
    assert got == {left, right}


def test_batch_as_a_workflow_without_after_keeps_strict_order(board):
    out = batch(board, [{"project": "MS", "title": "one"}, {"project": "MS", "title": "two"}],
                workflow_title="Chain")
    one, _ = out["ids"]
    assert core.next(board, "w1")["issue"]["id"] == one
    assert core.next(board, "w2") is None


def test_batch_replays_one_request_id_without_duplicates(board):
    items = [{"project": "MS", "title": "a", "ref": "a"}, {"project": "MS", "title": "b",
                                                             "after": ["a"]}]
    first = batch(board, items, workflow_title="W", request_id="r1")
    again = batch(board, items, workflow_title="W", request_id="r1")
    assert again == first
    assert issue_count(board) == 2
    with pytest.raises(core.Conflict):
        batch(board, items[:1], workflow_title="W", request_id="r1")


def test_batch_needs_at_least_one_item(board):
    with pytest.raises(core.BoardError):
        batch(board, [])


def test_a_bad_rank_in_a_batch_cannot_break_later_creates(board):
    with pytest.raises(core.BoardError):
        batch(board, [{"project": "MS", "title": "r", "rank": "high"}])
    assert core.create(board, "MS", "after", actor=CHAOS, actor_kind=HUMAN)["id"]


def test_batch_labels_are_stripped_like_the_web_form(board):
    (i,) = batch(board, [{"project": "MS", "title": "a", "labels": [" bug ", "ui"]}])["ids"]
    assert core.show(board, i)["labels"] == ["bug", "ui"]


def test_batch_rejects_a_non_string_workflow_title(board):
    with pytest.raises(core.BoardError):
        batch(board, [{"project": "MS", "title": "a"}], workflow_title=["W"])
    assert issue_count(board) == 0


def test_a_request_id_shaped_like_an_item_key_is_its_own_call(board):
    three = [{"project": "MS", "title": t} for t in "abc"]
    first = batch(board, three, request_id="r1")
    other = batch(board, [{"project": "MS", "title": t} for t in "xyz"], request_id="r1#1")
    assert None not in other["ids"] and not set(other["ids"]) & set(first["ids"])
    assert issue_count(board) == 6
    # The other order: the sub-key-shaped id goes first.
    batch(board, [{"project": "MS", "title": "p"}], request_id="s1#1")
    two = batch(board, [{"project": "MS", "title": "q"}, {"project": "MS", "title": "r"}],
                request_id="s1")
    assert None not in two["ids"] and issue_count(board) == 9


def test_a_three_item_cycle_names_every_item_in_it(board):
    items = [{"project": "MS", "title": "a", "after": [2]},
             {"project": "MS", "title": "b", "after": [0]},
             {"project": "MS", "title": "c", "after": [1]},
             {"project": "MS", "title": "d"}]
    with pytest.raises(core.BoardError, match=r"items 0, 1, 2 .*cycle"):
        batch(board, items)


def test_a_long_batch_chain_is_checked_without_recursion(board):
    items = [{"project": "MS", "title": f"s{i}", "after": [i - 1] if i else []}
             for i in range(1500)]
    items[0]["after"] = [1499]
    with pytest.raises(core.BoardError, match="cycle"):
        batch(board, items)


def test_a_long_workflow_title_still_replays(board):
    title = "W" * 300
    items = [{"project": "MS", "title": "a"}]
    first = batch(board, items, workflow_title=title, request_id="r1")
    assert batch(board, items, workflow_title=title, request_id="r1") == first


# --- project colour buckets (MS-655) ---------------------------------------------


def key_hash_bucket(key):
    import hashlib
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big") % 10


def test_new_projects_take_buckets_in_creation_order(migrated):
    for key in ("ZZ", "AA", "MM"):
        core.create_project(migrated, key, key.lower())
    assert core.colour_buckets(migrated) == {"ZZ": 0, "AA": 1, "MM": 2}


def test_a_new_project_takes_the_lowest_empty_bucket(migrated):
    for key in ("P0", "P1", "P2"):
        core.create_project(migrated, key, "p")
    core.delete_project(migrated, "P1")
    core.create_project(migrated, "NEW", "n")
    assert core.colour_buckets(migrated)["NEW"] == 1


def test_an_eleventh_project_takes_its_key_hash_bucket_whatever_came_first(migrated):
    ten = [f"P{n}" for n in range(10)]
    for order in (ten, ten[::-1]):
        for key in order:
            core.create_project(migrated, key, "p")
        assert sorted(core.colour_buckets(migrated).values()) == list(range(10))
        for key in ("ELEVEN", "TWELVE", "X"):
            core.create_project(migrated, key, "p")
            assert core.colour_buckets(migrated)[key] == key_hash_bucket(key)
        for key in core.colour_buckets(migrated):
            core.delete_project(migrated, key)


def test_a_rename_keeps_the_bucket(migrated):
    core.create_project(migrated, "AA", "a")
    core.create_project(migrated, "BB", "b")
    core.rename_project(migrated, "BB", "renamed")
    assert core.colour_buckets(migrated) == {"AA": 0, "BB": 1}
