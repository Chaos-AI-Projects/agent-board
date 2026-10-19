"""Stage and queue status, plan section 2 (AB-52).

A card's stage is its bottom lane; its queue status is ready, processing or
on-hold, or None when it is not queued. `move` sets the stage and `queue`
sets the queue status. A view reports both; the old single `state` is gone (AB-56).
"""

import pytest
from sqlalchemy import text

from board import core

AGENT = "agent"
HUMAN = "human"
CHAOS = "owner@example.com"


@pytest.fixture
def board(migrated):
    core.create_project(migrated, "MS", "memory-solution")
    return migrated


def stored(engine, issue_id):
    with engine.connect() as conn:
        return tuple(conn.execute(text("SELECT state, queue FROM issue WHERE id = :id"),
                                  {"id": issue_id}).one())


def where(view):
    return view["stage"], view["queue"]


def claim(engine):
    got = core.next(engine, AGENT)
    return got["issue"]["id"], got["lease_token"]


def test_a_ready_card_is_an_implement_card_queued_ready(board):
    issue = core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, stage="implement")
    assert where(issue) == ("implement", "ready")
    assert stored(board, issue["id"]) == ("implement", "ready")
    backlog = core.create(board, "MS", "y", actor=CHAOS, actor_kind=HUMAN)
    assert where(backlog) == ("backlog", None)


def test_create_takes_a_stage_and_queues_a_work_stage(board):
    issue = core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, stage="planning")
    assert where(issue) == ("planning", "ready")
    with pytest.raises(core.InvalidTransition):
        core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, stage="done")
    with pytest.raises(core.InvalidTransition):
        core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, stage="backlog",
                    queue="ready")


def test_next_claims_on_the_queue_and_records_the_queue_move(board):
    core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, stage="planning")
    got = core.next(board, "w1")
    assert where(got["issue"]) == ("planning", "processing")
    last = got["issue"]["events"][-1]
    assert (last["kind"], last["from_stage"], last["to_stage"]) == ("claim", "planning",
                                                                   "planning")
    assert (last["from_queue"], last["to_queue"]) == ("ready", "processing")


def test_moving_a_backlog_card_into_planning_queues_it(board):
    i = core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN)["id"]
    view = core.move(board, i, "planning", actor=CHAOS, actor_kind=HUMAN)
    assert where(view) == ("planning", "ready")
    view = core.move(board, i, "backlog", actor=CHAOS, actor_kind=HUMAN)
    assert where(view) == ("backlog", None)


@pytest.mark.parametrize("stage", ["backlog", "planning"])
def test_a_backlog_or_planning_card_moves_straight_to_done(board, stage):
    """AB-73: a card finished or overtaken before implement closes from its lane."""
    i = core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, stage=stage)["id"]
    view = core.move(board, i, "done", actor=CHAOS, actor_kind=HUMAN)
    assert where(view) == ("done", None)
    assert stored(board, i) == ("done", None)


def test_an_agent_closes_a_claimed_planning_card_with_a_note(board):
    core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, stage="planning")
    i, token = claim(board)
    with pytest.raises(core.NoteRequired):
        core.move(board, i, "done", actor=AGENT, actor_kind="agent", token=token)
    view = core.move(board, i, "done", actor=AGENT, actor_kind="agent", token=token,
                     note="already shipped")
    assert where(view) == ("done", None)
    assert view["lease_holder"] is None


def test_an_agent_releases_a_planning_card_to_wait_in_its_lane(board):
    core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, stage="planning")
    i, token = claim(board)
    with pytest.raises(core.NoteRequired):
        core.queue(board, i, None, actor=AGENT, actor_kind="agent", token=token)
    view = core.queue(board, i, None, actor=AGENT, actor_kind="agent", token=token,
                      note="plan at plans/x.md")
    assert where(view) == ("planning", None)
    assert view["lease_holder"] is None
    # Dragging it to implement is the approval: it is queued again.
    view = core.move(board, i, "implement", actor=CHAOS, actor_kind=HUMAN)
    assert where(view) == ("implement", "ready")


def test_the_queue_button_requeues_a_waiting_card(board):
    core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, stage="implement")
    i, token = claim(board)
    core.queue(board, i, None, actor=AGENT, actor_kind="agent", token=token, note="q")
    view = core.queue(board, i, "ready", actor=CHAOS, actor_kind=HUMAN)
    assert where(view) == ("implement", "ready")


def test_on_hold_comes_off_to_an_empty_queue(board):
    i = core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, stage="implement")["id"]
    assert where(core.queue(board, i, "on-hold", actor=CHAOS, actor_kind=HUMAN)) == (
        "implement", "on-hold")
    with pytest.raises(core.InvalidTransition):
        core.queue(board, i, "ready", actor=CHAOS, actor_kind=HUMAN)
    assert where(core.queue(board, i, None, actor=CHAOS, actor_kind=HUMAN)) == (
        "implement", None)


def test_queue_refuses_processing_and_unqueued_stages(board):
    i = core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN)["id"]
    with pytest.raises(core.InvalidTransition):
        core.queue(board, i, "ready", actor=CHAOS, actor_kind=HUMAN)
    j = core.create(board, "MS", "y", actor=CHAOS, actor_kind=HUMAN, stage="implement")["id"]
    with pytest.raises(core.InvalidTransition):
        core.queue(board, j, "processing", actor=CHAOS, actor_kind=HUMAN)


def test_closing_a_claimed_card_ends_its_queue_and_reopen_requeues_it(board):
    core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, stage="implement")
    i, token = claim(board)
    view = core.move(board, i, "done", actor=AGENT, actor_kind="agent", token=token,
                     note="PR 1")
    assert where(view) == ("done", None)
    with pytest.raises(core.NoteRequired):
        core.move(board, i, "implement", actor=AGENT, actor_kind="agent")
    view = core.move(board, i, "implement", actor=AGENT, actor_kind="agent", note="regressed")
    assert where(view) == ("implement", "ready")


def test_stats_count_a_claim_as_a_move(board):
    core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, stage="implement")
    core.next(board, "w1")
    w1 = [a for a in core.stats(board)["actors"] if a["actor"] == "w1"]
    assert [a["moved"] for a in w1] == [1]


def test_a_card_waiting_on_an_answer_can_go_on_hold(board):
    # Plan section 2: any queue status to on-hold, empty included.
    core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, stage="implement")
    i, token = claim(board)
    core.queue(board, i, None, actor=AGENT, actor_kind="agent", token=token, note="q")
    assert where(core.queue(board, i, "on-hold", actor=CHAOS, actor_kind=HUMAN)) == (
        "implement", "on-hold")


def test_no_view_reports_the_old_single_state(board):
    """AB-56: a view carries stage and queue; the folded `state` name is gone."""
    a = core.create(board, "MS", "a", actor=CHAOS, actor_kind=HUMAN, stage="implement")
    b = core.create(board, "MS", "b", actor=CHAOS, actor_kind=HUMAN, stage="implement")
    core.depend(board, b["id"], a["id"], actor=CHAOS, actor_kind=HUMAN)
    core.annotate(board, a["id"], "n", actor=CHAOS, actor_kind=HUMAN)
    view = core.show(board, b["id"])
    assert "state" not in view
    assert "state" not in view["depends_on"][0]
    assert "state" not in core.show(board, a["id"])["blocks"][0]
    for e in core.show(board, a["id"])["events"]:
        assert "from_state" not in e and "to_state" not in e
    assert all("state" not in row for row in core.search(board))
    for e in core.stats(board)["activity"]:
        assert "from_state" not in e and "to_state" not in e
        assert {"from_stage", "to_stage", "from_queue", "to_queue"} <= e.keys()
    assert not hasattr(core, "legacy_state") and not hasattr(core, "_LEGACY_FOLD")
