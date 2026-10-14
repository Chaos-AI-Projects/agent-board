"""Cron-scheduled cards, brain plans/agent-board-lanes.md section 4 (AB-54).

A card's schedule is five cron fields read in its own zone, `schedule_tz`.
Due times are computed in that zone and stored in UTC. `next` skips a ready
card whose `next_run_at` is still ahead, advances it on claim, and a finished
run goes back to ready instead of done.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from board import core
from board.store import Issue

AGENT = "agent"
HUMAN = "human"
CHAOS = "owner@example.com"
SYDNEY = "Australia/Sydney"


@pytest.fixture
def board(migrated):
    core.create_project(migrated, "MS", "memory-solution")
    return migrated


def set_next_run(engine, issue_id, at):
    with engine.begin() as conn:
        conn.execute(update(Issue).where(Issue.id == issue_id).values(next_run_at=at))


def scheduled(engine, expr="0 9 * * *", tz=SYDNEY, **kw):
    return core.create(engine, "MS", "nightly", actor=CHAOS, actor_kind=HUMAN,
                       state="ready", schedule=expr, timezone=tz, **kw)


def test_next_due_reads_the_fields_in_the_cards_zone():
    # Sydney's daylight saving began at 02:00 on Sunday 2026-10-04.
    before = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)   # 10:00 AEST
    assert core.next_due("0 9 * * *", SYDNEY, before) == datetime(
        2026, 10, 2, 23, 0, tzinfo=timezone.utc)                 # 09:00 AEST on the 3rd
    after = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)    # 11:00 AEDT
    assert core.next_due("0 9 * * *", SYDNEY, after) == datetime(
        2026, 10, 5, 22, 0, tzinfo=timezone.utc)                 # 09:00 AEDT on the 6th


def test_next_due_is_strictly_after_the_given_time():
    at = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
    assert core.next_due("0 9 * * *", "UTC", at) == at + timedelta(days=1)


@pytest.mark.parametrize("expr", ["", "* * * *", "* * * * * *", "61 * * * *", "nonsense"])
def test_a_bad_expression_is_refused(board, expr):
    with pytest.raises(core.BoardError):
        scheduled(board, expr=expr)


def test_an_unknown_zone_is_refused_not_skipped(board):
    with pytest.raises(core.BoardError, match="zone"):
        scheduled(board, tz="Mars/Olympus")


def test_create_with_a_schedule_stores_it_and_its_next_due_time(board):
    issue = scheduled(board)
    assert issue["schedule"] == "0 9 * * *"
    assert issue["schedule_tz"] == SYDNEY
    due = datetime.fromisoformat(issue["next_run_at"])
    now = datetime.fromisoformat(core.now(board))
    assert now < due <= now + timedelta(days=1)
    local = due.astimezone(ZoneInfo(SYDNEY))
    assert (local.hour, local.minute) == (9, 0)


def test_a_schedule_without_a_zone_is_refused(board):
    with pytest.raises(core.BoardError):
        scheduled(board, tz=None)


def test_next_skips_a_scheduled_card_until_it_is_due(board):
    issue = scheduled(board)
    assert core.next(board, AGENT) is None
    set_next_run(board, issue["id"], datetime.now(timezone.utc) - timedelta(minutes=1))
    got = core.next(board, AGENT)
    assert got["issue"]["id"] == issue["id"]


def test_claim_advances_next_run_and_missed_fires_collapse(board):
    issue = scheduled(board, expr="*/5 * * * *", tz="UTC")
    set_next_run(board, issue["id"], datetime.now(timezone.utc) - timedelta(hours=3))
    got = core.next(board, AGENT)
    due = datetime.fromisoformat(got["issue"]["next_run_at"])
    now = datetime.fromisoformat(core.now(board))
    assert now < due <= now + timedelta(minutes=5)


def test_a_finished_run_goes_back_to_ready_not_done(board):
    issue = scheduled(board)
    set_next_run(board, issue["id"], datetime.now(timezone.utc) - timedelta(minutes=1))
    token = core.next(board, AGENT)["lease_token"]
    after = core.transition(board, issue["id"], "done", actor=AGENT, actor_kind=AGENT,
                            note="ran", token=token)
    assert (after["stage"], after["queue"]) == ("implement", "ready")
    assert after["lease_holder"] is None
    assert core.next(board, AGENT) is None    # waits for its next due time

    set_next_run(board, issue["id"], datetime.now(timezone.utc) - timedelta(minutes=1))
    token = core.next(board, AGENT)["lease_token"]
    after = core.move(board, issue["id"], "done", actor=AGENT, actor_kind=AGENT,
                      note="ran", token=token)
    assert (after["stage"], after["queue"]) == ("implement", "ready")


def test_a_scheduled_card_can_still_be_cancelled(board):
    issue = scheduled(board)
    after = core.move(board, issue["id"], "cancelled", actor=CHAOS, actor_kind=HUMAN)
    assert after["stage"] == "cancelled"


def test_set_schedule_changes_and_clears_it(board):
    issue = core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, state="ready")
    assert issue["schedule"] is None and issue["next_run_at"] is None
    after = core.set_schedule(board, issue["id"], "30 6 * * 1", timezone=SYDNEY,
                              actor=CHAOS, actor_kind=HUMAN)
    assert (after["schedule"], after["schedule_tz"]) == ("30 6 * * 1", SYDNEY)
    first = after["next_run_at"]
    assert first is not None
    moved = core.set_schedule(board, issue["id"], "30 6 * * 1", timezone="UTC",
                              actor=CHAOS, actor_kind=HUMAN)
    assert moved["schedule_tz"] == "UTC" and moved["next_run_at"] != first
    assert moved["events"][-1]["kind"] == "schedule"
    cleared = core.set_schedule(board, issue["id"], None, timezone=None,
                                actor=CHAOS, actor_kind=HUMAN)
    assert (cleared["schedule"], cleared["schedule_tz"], cleared["next_run_at"]) == (
        None, None, None)
    assert core.next(board, AGENT)["issue"]["id"] == issue["id"]


def test_an_agent_sets_a_schedule_only_under_its_lease(board):
    issue = core.create(board, "MS", "x", actor=CHAOS, actor_kind=HUMAN, state="ready")
    token = core.next(board, AGENT)["lease_token"]
    with pytest.raises(core.LeaseLost):
        core.set_schedule(board, issue["id"], "0 9 * * *", timezone="UTC",
                          actor=AGENT, actor_kind=AGENT, token="stale")
    after = core.set_schedule(board, issue["id"], "0 9 * * *", timezone="UTC",
                              actor=AGENT, actor_kind=AGENT, token=token)
    assert after["schedule"] == "0 9 * * *"


def test_a_date_that_never_comes_is_refused(board):
    with pytest.raises(core.BoardError, match="never"):
        scheduled(board, expr="0 9 31 2 *")
