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


def place(view):
    """A view's stage and queue status, the pair the old single state folded."""
    return view["stage"], view["queue"]

CHAOS = "owner@example.com"


@pytest.fixture
def url(tmp_path):
    url = f"sqlite:///{tmp_path / 'board.db'}"
    eng = store.make_engine(url)
    store.upgrade(eng)
    core.create_project(eng, "MS", "memory-solution")
    eng.dispose()
    return url


def board(url, *args, actor="worker-1", stdin=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOARD_")}
    if url is not None:
        env[store.DATABASE_URL_ENV] = url
    if actor is not None:
        env["BOARD_ACTOR"] = actor
    return subprocess.run([sys.executable, "-m", "board.cli", *args],
                          capture_output=True, text=True, env=env, timeout=60, input=stdin)


def ok(proc):
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def ready(url, title="item"):
    return ok(board(url, "--actor-kind", "human", "create", "--project", "MS",
                    "--title", title, "--stage", "implement", actor=CHAOS))["id"]


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
    assert place(out["issue"]) == ("implement", "processing")
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


def test_a_human_queue_change_under_a_live_lease_exits_5(url):
    issue_id = ready(url)
    claim(url)
    proc = board(url, "--actor-kind", "human", "queue", issue_id, "on-hold",
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


def test_link_takes_a_name_for_a_url(url):
    issue_id = ready(url)
    token = claim(url)["lease_token"]
    out = ok(board(url, "link", issue_id, "--artifact", "https://example.com/d", "--kind", "url",
                   "--name", "Design doc", "--token", token))
    assert [(a["ref"], a["name"]) for a in out["artifacts"]] == [
        ("https://example.com/d", "Design doc")]


def test_an_item_worked_to_done_through_the_cli(url):
    issue_id = ready(url)
    token = claim(url)["lease_token"]
    out = ok(board(url, "heartbeat", issue_id, "--token", token, "--ttl", "30"))
    assert out["lease_expires_at"]
    ok(board(url, "annotate", issue_id, "--note", "halfway", "--token", token))
    ok(board(url, "link", issue_id, "--artifact", "abc123", "--kind", "commit",
             "--closes", "--token", token))
    done = ok(board(url, "move", issue_id, "done", "--note", "shipped",
                    "--token", token))
    assert place(done) == ("done", None)
    assert done["closed_by_artifact"] is True
    kinds = [e["kind"] for e in done["events"]]
    assert kinds[-3:] == ["annotate", "link", "transition"]


def test_done_without_a_note_is_refused(url):
    issue_id = ready(url)
    token = claim(url)["lease_token"]
    proc = board(url, "move", issue_id, "done", "--token", token)
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"] == "NoteRequired"


def test_create_takes_labels_body_and_rank(url):
    out = ok(board(url, "--actor-kind", "human", "create", "--project", "MS",
                   "--title", "t", "--body", "b", "--rank", "7", "--label", "x",
                   "--label", "y", actor=CHAOS))
    assert (out["body"], out["rank"], out["labels"], out["stage"]) == ("b", 7, ["x", "y"], "backlog")


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
        ok(board(url, "queue", issue_id, "none", "--note", "waiting",
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


def test_ab27_search_by_creator(url):
    mine = ok(board(url, "--actor-kind", "human", "create", "--project", "MS",
                    "--title", "mine", actor=CHAOS))["id"]
    ok(board(url, "create", "--project", "MS", "--title", "theirs", actor="overlord"))
    out = ok(board(url, "search", "--creator", CHAOS, actor=None))
    assert [i["id"] for i in out["issues"]] == [mine]



def test_ab70_search_by_involved(url):
    theirs = ok(board(url, "create", "--project", "MS", "--title", "theirs",
                      actor="overlord"))["id"]
    ok(board(url, "--actor-kind", "human", "annotate", theirs, "--note", "looking", actor=CHAOS))
    ok(board(url, "create", "--project", "MS", "--title", "untouched", actor="overlord"))
    out = ok(board(url, "search", "--involved", CHAOS, actor=None))
    assert [i["id"] for i in out["issues"]] == [theirs]

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
    assert place(out) == ("implement", "on-hold")
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


def test_hold_and_resume_a_plan_by_its_parent(url):
    issue_id = ready(url, "big job")
    token = claim(url)["lease_token"]
    step = ok(board(url, "plan", issue_id, "--step", "a", "--token", token))["plan"]["steps"][0]
    assert ok(board(url, "hold", issue_id))["plan"]["held"] is True
    assert board(url, "next").returncode == 3
    assert ok(board(url, "resume", issue_id))["plan"]["held"] is False
    assert claim(url)["issue"]["id"] == step["id"]


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


# --- create-batch (MS-647) ----------------------------------------------------


BATCH = {"workflow_title": "Fan out",
         "items": [{"project": "MS", "title": "design", "ref": "d"},
                   {"project": "MS", "title": "left", "after": ["d"]},
                   {"project": "MS", "title": "right", "after": [0]}]}


def test_create_batch_reads_json_from_stdin(url):
    out = ok(board(url, "create-batch", "-", stdin=json.dumps(BATCH)))
    design, left, right = out["ids"]
    assert out["refs"] == {"d": design}
    assert out["workflow_id"] is not None
    assert [d["id"] for d in ok(board(url, "show", right))["depends_on"]] == [design]


def test_create_batch_reads_a_file_and_replays_a_request_id(url, tmp_path):
    f = tmp_path / "batch.json"
    f.write_text(json.dumps(BATCH))
    first = ok(board(url, "create-batch", str(f), "--request-id", "r1"))
    assert ok(board(url, "create-batch", str(f), "--request-id", "r1")) == first


def test_create_batch_takes_a_bare_list_of_items(url):
    out = ok(board(url, "create-batch", "-", stdin=json.dumps(BATCH["items"])))
    assert len(out["ids"]) == 3
    assert out["workflow_id"] is None


def test_a_bad_batch_is_code_1_and_writes_nothing(url):
    bad = {"items": [{"project": "MS", "title": "a"}, {"project": "MS", "title": ""}]}
    proc = board(url, "create-batch", "-", stdin=json.dumps(bad))
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"] == "BoardError"
    assert board(url, "search").returncode == 3


def test_create_batch_rejects_malformed_json_as_code_1(url):
    proc = board(url, "create-batch", "-", stdin="{not json")
    assert proc.returncode == 1
    assert "error" in json.loads(proc.stderr)


def test_ab24_an_agent_reopens_a_done_card_without_a_token(url):
    issue_id = ready(url)
    token = claim(url)["lease_token"]
    ok(board(url, "move", issue_id, "done", "--note", "merged", "--token", token))
    refused = board(url, "move", issue_id, "implement")
    assert refused.returncode != 0
    assert json.loads(refused.stderr)["error"] == "NoteRequired"
    out = ok(board(url, "move", issue_id, "implement", "--note", "regressed"))
    assert place(out) == ("implement", "ready")
    assert claim(url, actor="worker-2")["issue"]["id"] == issue_id


# --- AB-52: stage and queue -------------------------------------------------


def test_ab52_create_takes_a_stage_and_queues_a_work_stage(url):
    out = ok(board(url, "create", "--project", "MS", "--title", "p", "--stage", "planning"))
    assert (out["stage"], out["queue"]) == ("planning", "ready")
    out = ok(board(url, "create", "--project", "MS", "--title", "b", "--stage", "backlog"))
    assert (out["stage"], out["queue"]) == ("backlog", None)


def test_ab52_create_refuses_a_queue_outside_a_work_stage(url):
    proc = board(url, "create", "--project", "MS", "--title", "b", "--stage", "backlog",
                 "--queue", "ready")
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"] == "InvalidTransition"


def test_ab52_move_sets_the_stage_under_the_lease(url):
    ready(url)
    out = claim(url)
    issue_id, token = out["issue"]["id"], out["lease_token"]
    out = ok(board(url, "move", issue_id, "planning", "--token", token))
    assert (out["stage"], out["queue"]) == ("planning", "ready")
    assert out["lease_holder"] is None


def test_ab52_move_into_done_needs_a_note(url):
    ready(url)
    out = claim(url)
    issue_id, token = out["issue"]["id"], out["lease_token"]
    proc = board(url, "move", issue_id, "done", "--token", token)
    assert proc.returncode == 1
    assert json.loads(proc.stderr)["error"] == "NoteRequired"
    out = ok(board(url, "move", issue_id, "done", "--token", token, "--note", "abc123"))
    assert (out["stage"], out["queue"]) == ("done", None)


def test_ab52_queue_holds_and_releases_a_claimed_card(url):
    ready(url)
    out = claim(url)
    issue_id, token = out["issue"]["id"], out["lease_token"]
    out = ok(board(url, "queue", issue_id, "on-hold", "--token", token, "--note", "wait"))
    assert (out["stage"], out["queue"]) == ("implement", "on-hold")


def test_ab52_queue_empty_takes_a_claimed_card_out_of_the_queue(url):
    ready(url)
    out = claim(url)
    issue_id, token = out["issue"]["id"], out["lease_token"]
    out = ok(board(url, "queue", issue_id, "none", "--token", token, "--note", "a question"))
    assert (out["stage"], out["queue"]) == ("implement", None)


def test_ab52_queue_refuses_processing_as_a_usage_error(url):
    issue_id = ready(url)
    assert board(url, "queue", issue_id, "processing").returncode == 2


def test_create_takes_a_schedule_read_in_board_timezone_by_default(url):
    env_tz = {"BOARD_TIMEZONE": "Australia/Sydney"}
    proc = board_env(url, env_tz, "--actor-kind", "human", "create", "--project", "MS",
                     "--title", "nightly", "--stage", "implement", "--schedule", "0 9 * * *",
                     actor=CHAOS)
    issue = ok(proc)
    assert (issue["schedule"], issue["schedule_tz"]) == ("0 9 * * *", "Australia/Sydney")
    utc = ok(board(url, "--actor-kind", "human", "create", "--project", "MS",
                   "--title", "x", "--stage", "implement", "--schedule", "0 9 * * *",
                   actor=CHAOS))
    assert utc["schedule_tz"] == "UTC"
    assert board(url, "next").returncode == 3


def test_schedule_sets_and_clears_and_refuses_an_unknown_zone(url):
    issue_id = ready(url)
    got = ok(board(url, "--actor-kind", "human", "schedule", issue_id, "--cron",
                   "*/15 * * * *", "--timezone", "Europe/London", actor=CHAOS))
    assert (got["schedule"], got["schedule_tz"]) == ("*/15 * * * *", "Europe/London")
    bad = board(url, "--actor-kind", "human", "schedule", issue_id, "--cron", "0 9 * * *",
                "--timezone", "Mars/Olympus", actor=CHAOS)
    assert bad.returncode == 1 and "zone" in bad.stderr
    often = board(url, "--actor-kind", "human", "schedule", issue_id, "--cron", "* * * * *",
                  "--timezone", "UTC", actor=CHAOS)
    assert often.returncode == 1 and "5 minutes" in often.stderr
    cleared = ok(board(url, "--actor-kind", "human", "schedule", issue_id, "--clear",
                       actor=CHAOS))
    assert cleared["schedule"] is None and cleared["next_run_at"] is None


def board_env(url, extra, *args, actor="worker-1"):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOARD_")}
    env.update(extra, **{store.DATABASE_URL_ENV: url, "BOARD_ACTOR": actor})
    return subprocess.run([sys.executable, "-m", "board.cli", *args],
                          capture_output=True, text=True, env=env, timeout=60)


# --- AB-55: the transition shim is gone ------------------------------------------


def test_ab55_transition_is_no_longer_a_command(url):
    proc = board(url, "transition", "MS-1", "ready")
    assert proc.returncode == 2
    assert "invalid choice" in proc.stderr


def test_ab55_create_takes_no_state(url):
    proc = board(url, "create", "--project", "MS", "--title", "t", "--state", "ready")
    assert proc.returncode == 2
    out = ok(board(url, "create", "--project", "MS", "--title", "t"))
    assert (out["stage"], out["queue"]) == ("backlog", None)


def test_ab63_create_names_the_person_an_agent_enters_the_card_for(url):
    out = ok(board(url, "--actor-kind", "agent", "create", "--project", "MS",
                   "--title", "his ask", "--creator", CHAOS, actor="overlord"))
    assert (out["creator"], out["creator_kind"], out["created_by_agent"]) == (
        CHAOS, "human", "overlord")
    assert out["events"][0]["actor"] == "overlord"
    assert out["events"][0]["on_behalf_of"] == CHAOS


def test_ab63_a_human_cannot_name_a_creator(url):
    proc = board(url, "--actor-kind", "human", "create", "--project", "MS",
                 "--title", "t", "--creator", "someone@example.com", actor=CHAOS)
    assert proc.returncode == 1
    assert "only an agent" in json.loads(proc.stderr)["message"]


def test_ab69_instantiate_and_create_batch_take_a_creator(url):
    eng = store.make_engine(url)
    core.create_template(eng, "ship", "Ship it", ["build"])
    eng.dispose()
    wf = ok(board(url, "--actor-kind", "agent", "instantiate", "ship", "--project", "MS",
                  "--creator", CHAOS, actor="overlord"))
    batch = ok(board(url, "--actor-kind", "agent", "create-batch", "-", "--creator", CHAOS,
                     actor="overlord", stdin=json.dumps([{"project": "MS", "title": "a"}])))
    for i in [wf["steps"][0]["id"], *batch["ids"]]:
        out = ok(board(url, "show", i))
        assert (out["creator"], out["created_by_agent"]) == (CHAOS, "overlord")
