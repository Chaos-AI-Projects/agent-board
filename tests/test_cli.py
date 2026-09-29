"""The `board` CLI as a cron job sees it: a subprocess, JSON and an exit code.

Every test runs the CLI in a child process, because the exit code is the
contract (design section 1): 0 ok, 3 empty queue, 4 lease lost, 5 conflict.
SQLite only; the engine-specific behaviour lives in test_core.py.
"""

import json
import os
import subprocess
import sys

import pytest

from board import core, store

CHAOS = "owner@example.com"


@pytest.fixture
def url(tmp_path):
    url = f"sqlite:///{tmp_path / 'board.db'}"
    eng = store.make_engine(url)
    store.upgrade(eng)
    core.create_project(eng, "MS", "memory-solution")
    eng.dispose()
    return url


def board(url, *args, actor="worker-1"):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOARD_")}
    if url is not None:
        env[store.DATABASE_URL_ENV] = url
    if actor is not None:
        env["BOARD_ACTOR"] = actor
    return subprocess.run([sys.executable, "-m", "board.cli", *args],
                          capture_output=True, text=True, env=env, timeout=60)


def ok(proc):
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def ready(url, title="item"):
    return ok(board(url, "--actor-kind", "human", "create", "--project", "MS",
                    "--title", title, "--state", "ready", actor=CHAOS))["id"]


def claim(url, actor="worker-1"):
    return ok(board(url, "next", actor=actor))


def events(url, issue_id):
    return ok(board(url, "show", issue_id))["events"]


# --- exit codes --------------------------------------------------------------


def test_next_on_an_empty_queue_exits_3(url):
    proc = board(url, "next")
    assert proc.returncode == 3, proc.stderr
    assert json.loads(proc.stdout) == {"issue": None}


def test_next_claims_and_prints_issue_and_token(url):
    issue_id = ready(url)
    out = claim(url)
    assert out["issue"]["id"] == issue_id
    assert out["issue"]["state"] == "processing"
    assert out["issue"]["lease_holder"] == "worker-1"
    assert out["lease_token"]


def test_a_stale_token_exits_4(url):
    issue_id = ready(url)
    claim(url)
    proc = board(url, "annotate", issue_id, "--note", "hi", "--token", "stale")
    assert proc.returncode == 4
    assert json.loads(proc.stderr)["error"] == "LeaseLost"


def test_heartbeat_by_a_non_holder_exits_4(url):
    issue_id = ready(url)
    token = claim(url)["lease_token"]
    proc = board(url, "heartbeat", issue_id, "--token", token, actor="worker-2")
    assert proc.returncode == 4


def test_a_human_transition_under_a_live_lease_exits_5(url):
    issue_id = ready(url)
    claim(url)
    proc = board(url, "--actor-kind", "human", "transition", issue_id, "ready",
                 actor=CHAOS)
    assert proc.returncode == 5
    assert json.loads(proc.stderr)["error"] == "LeaseHeld"


def test_a_request_id_reused_with_other_arguments_exits_5(url):
    issue_id = ready(url)
    token = claim(url)["lease_token"]
    ok(board(url, "annotate", issue_id, "--note", "a", "--token", token,
             "--request-id", "r1"))
    proc = board(url, "annotate", issue_id, "--note", "b", "--token", token,
                 "--request-id", "r1")
    assert proc.returncode == 5


def test_other_board_errors_exit_1(url):
    proc = board(url, "show", "MS-999")
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"] == "NotFound"


def test_a_missing_database_url_exits_1(url):
    proc = board(None, "show", "MS-1")
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"] == "ConfigError"


def test_a_missing_actor_is_a_usage_error(url):
    proc = board(url, "next", actor=None)
    assert proc.returncode == 2
    assert "--actor" in proc.stderr


# --- the operations end to end -------------------------------------------------


def test_an_item_worked_to_done_through_the_cli(url):
    issue_id = ready(url)
    token = claim(url)["lease_token"]
    out = ok(board(url, "heartbeat", issue_id, "--token", token, "--ttl", "30"))
    assert out["lease_expires_at"]
    ok(board(url, "annotate", issue_id, "--note", "halfway", "--token", token))
    ok(board(url, "link", issue_id, "--artifact", "abc123", "--kind", "commit",
             "--closes", "--token", token))
    done = ok(board(url, "transition", issue_id, "done", "--note", "shipped",
                    "--token", token))
    assert done["state"] == "done"
    assert done["closed_by_artifact"] is True
    kinds = [e["kind"] for e in done["events"]]
    assert kinds[-3:] == ["annotate", "link", "transition"]


def test_done_without_a_note_is_refused(url):
    issue_id = ready(url)
    token = claim(url)["lease_token"]
    proc = board(url, "transition", issue_id, "done", "--token", token)
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"] == "NoteRequired"


def test_create_takes_labels_body_and_rank(url):
    out = ok(board(url, "--actor-kind", "human", "create", "--project", "MS",
                   "--title", "t", "--body", "b", "--rank", "7", "--label", "x",
                   "--label", "y", actor=CHAOS))
    assert (out["body"], out["rank"], out["labels"], out["state"]) == ("b", 7, ["x", "y"], "backlog")


def test_instantiate_creates_the_workflow(url):
    eng = store.make_engine(url)
    core.create_template(eng, "ship", "Ship it", ["build", "review"])
    eng.dispose()
    out = ok(board(url, "--actor-kind", "human", "instantiate", "ship", "--project", "MS",
                   actor=CHAOS))
    assert [s["title"] for s in out["steps"]] == ["build", "review"]


def test_next_honours_the_project_filter(url):
    ready(url)
    proc = board(url, "next", "--project", "NOPE")
    assert proc.returncode == 3


# --- idempotency (design section 8) -------------------------------------------


def test_an_exact_retry_under_one_lease_collapses_without_a_request_id(url):
    issue_id = ready(url)
    token = claim(url)["lease_token"]
    for _ in range(2):
        ok(board(url, "annotate", issue_id, "--note", "same", "--token", token))
    assert [e["kind"] for e in events(url, issue_id)].count("annotate") == 1


def test_a_request_id_replays_the_first_call(url):
    issue_id = ready(url)
    token = claim(url)["lease_token"]
    for _ in range(2):
        ok(board(url, "transition", issue_id, "need-input", "--note", "waiting",
                 "--token", token, "--request-id", "t1"))
    assert [e["kind"] for e in events(url, issue_id)].count("transition") == 1


def test_a_retried_next_with_a_request_id_returns_the_same_claim(url):
    ready(url, "a")
    ready(url, "b")
    first = ok(board(url, "next", "--request-id", "n1"))
    again = ok(board(url, "next", "--request-id", "n1"))
    assert again["issue"]["id"] == first["issue"]["id"]
    assert again["lease_token"] == first["lease_token"]


def test_migrate_brings_a_fresh_database_to_head(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    proc = board(url, "migrate")
    assert proc.returncode == 0, proc.stderr
    eng = store.make_engine(url)
    core.create_project(eng, "MS", "memory-solution")
    eng.dispose()


# --- errors outside board.core ------------------------------------------------


def test_a_database_error_is_a_json_error_not_a_traceback(tmp_path):
    proc = board(f"sqlite:///{tmp_path / 'unmigrated.db'}", "show", "MS-1")
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"] == "OperationalError"


@pytest.mark.parametrize("ttl", ["0", "-5", "inf", "nan", "1e12"])
def test_an_unusable_ttl_is_a_usage_error(url, ttl):
    ready(url)
    proc = board(url, "next", "--ttl", ttl)
    assert proc.returncode == 2, proc.stderr


def test_the_actor_may_follow_the_subcommand(url):
    ready(url)
    out = ok(board(url, "next", "--actor", "worker-9", actor=None))
    assert out["issue"]["lease_holder"] == "worker-9"


def test_next_claims_only_as_an_agent(url):
    ready(url)
    proc = board(url, "--actor-kind", "human", "next")
    assert proc.returncode == 2


@pytest.mark.parametrize("bad", ["garbage", "foo://u@h/db", "mysql://u@h/db"])
def test_an_unusable_database_url_is_a_json_error(bad):
    proc = board(bad, "show", "MS-1")
    assert proc.returncode == 1
    assert "error" in json.loads(proc.stderr)


def test_an_oversized_rank_is_a_json_error(url):
    proc = board(url, "--actor-kind", "human", "create", "--project", "MS", "--title", "z",
                 "--rank", str(10**20), actor=CHAOS)
    assert proc.returncode == 1
    assert "error" in json.loads(proc.stderr)


def test_the_actor_may_precede_the_subcommand(url):
    ready(url)
    out = ok(board(url, "--actor", "worker-8", "next", actor=None))
    assert out["issue"]["lease_holder"] == "worker-8"


def test_show_needs_no_actor(url):
    issue_id = ready(url)
    assert ok(board(url, "show", issue_id, actor=None))["id"] == issue_id


def test_create_project_prints_it_and_takes_a_first_card(tmp_path):
    fresh = f"sqlite:///{tmp_path / 'fresh.db'}"
    ok(board(fresh, "migrate", actor=None))
    out = ok(board(fresh, "create-project", "BR", "brain", actor=None))
    assert out == {"key": "BR", "name": "brain"}
    card = ok(board(fresh, "--actor-kind", "human", "create", "--project", "BR",
                    "--title", "first", actor=CHAOS))
    assert card["id"] == "BR-1"


def test_create_project_with_a_taken_key_exits_5(url):
    proc = board(url, "create-project", "MS", "again")
    assert proc.returncode == 5
    assert json.loads(proc.stderr)["error"] == "Conflict"


# --- search (MS-629) ---------------------------------------------------------


def test_search_prints_matching_issues_and_needs_no_actor(url):
    hit = ready(url, "Search box")
    ready(url, "Lease expiry")
    out = ok(board(url, "search", "SEARCH", actor=None))
    assert [i["id"] for i in out["issues"]] == [hit]


def test_search_filters_combine_with_the_query(url):
    ok(board(url, "--actor-kind", "human", "create", "--project", "MS", "--title", "one",
             "--label", "web", actor=CHAOS))
    two = ok(board(url, "--actor-kind", "human", "create", "--project", "MS",
                   "--title", "two", "--label", "web", actor=CHAOS))["id"]
    out = ok(board(url, "search", "two", "--project", "MS", "--label", "web", actor=None))
    assert [i["id"] for i in out["issues"]] == [two]
    out = ok(board(url, "search", "--label", "web", actor=None))
    assert len(out["issues"]) == 2


def test_search_with_no_hits_exits_3(url):
    ready(url, "Search box")
    proc = board(url, "search", "nothing", "--assignee", "nobody", actor=None)
    assert proc.returncode == 3, proc.stderr
    assert json.loads(proc.stdout) == {"issues": []}


# --- MS-644: plan ------------------------------------------------------------


def test_plan_turns_the_claimed_issue_into_ordered_steps(url):
    issue_id = ready(url, "big job")
    token = claim(url)["lease_token"]
    out = ok(board(url, "plan", issue_id, "--step", "design", "--step", "build",
                   "--token", token))
    assert out["state"] == "onhold"
    assert [(s["position"], s["title"]) for s in out["plan"]["steps"]] == [
        (1, "design"), (2, "build")]


def test_plan_without_a_step_is_a_usage_error(url):
    assert board(url, "plan", "MS-1").returncode == 2


def test_a_second_plan_exits_5(url):
    issue_id = ready(url)
    token = claim(url)["lease_token"]
    ok(board(url, "plan", issue_id, "--step", "a", "--token", token))
    proc = board(url, "--actor-kind", "human", "plan", issue_id, "--step", "b", actor=CHAOS)
    assert proc.returncode == 5, proc.stderr


# --- dependencies (MS-646) ---------------------------------------------------


def test_depend_holds_an_issue_back_until_its_dependency_is_done(url):
    first, second = ready(url, "first"), ready(url, "second")
    out = ok(board(url, "depend", first, "--on", second))
    assert [d["id"] for d in out["depends_on"]] == [second]
    assert claim(url)["issue"]["id"] == second
    assert board(url, "next", actor="worker-2").returncode == 3


def test_undepend_releases_the_issue(url):
    first, second = ready(url, "first"), ready(url, "second")
    ok(board(url, "depend", first, "--on", second))
    out = ok(board(url, "undepend", first, "--on", second))
    assert out["depends_on"] == []
    assert [e["kind"] for e in events(url, first)][-2:] == ["depend", "undepend"]


def test_a_dependency_cycle_is_a_json_error(url):
    first, second = ready(url, "first"), ready(url, "second")
    ok(board(url, "depend", first, "--on", second))
    proc = board(url, "depend", second, "--on", first)
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"] == "BoardError"


def test_undepend_of_a_missing_edge_is_a_json_error(url):
    first, second = ready(url, "first"), ready(url, "second")
    proc = board(url, "undepend", first, "--on", second)
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"] == "NotFound"


def test_depend_needs_on(url):
    assert board(url, "depend", ready(url)).returncode == 2
