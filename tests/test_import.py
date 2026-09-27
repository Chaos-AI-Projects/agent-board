"""`board import-backlog` and PRD section 9 criterion 6 (design sections 9 and 10).

The parity tests run both real commands as subprocesses on the same files:
brain's `backlog.py next` and `board next` on a database that
`board import-backlog` filled from them. SQLite only, as design section 10
row 6 says. They skip when brain's backlog.py is absent, because reading the
files through that parser, and no copy of it, is the point of the import.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from board import store

BACKLOG_PY = Path(os.environ.get("BACKLOG_PY", "/home/overlord/brain/backlog.py"))
LIVE_BACKLOG = BACKLOG_PY.parent / "backlog"

pytestmark = pytest.mark.skipif(
    not BACKLOG_PY.exists(), reason=f"brain backlog.py NOT FOUND at {BACKLOG_PY}; set BACKLOG_PY")


def item(ident, title, state, *notes, origin="email"):
    lines = [f"### {ident} -- {title}", f"- state: {state}", f"- origin: {origin}"]
    lines += [f"- notes: {n}" for n in notes]
    return "\n".join(lines) + "\n\n"


def write_backlog(root, **files):
    root.mkdir(exist_ok=True)
    for stem, items in files.items():
        (root / f"{stem.replace('_', '-')}.md").write_text(f"# {stem}\n\n" + "".join(items))
    return root


def backlog_next(root):
    proc = subprocess.run([sys.executable, str(BACKLOG_PY), "next", "--root", str(root)],
                          capture_output=True, text=True, timeout=60)
    item = json.loads(proc.stdout)["item"]
    return proc.returncode, item and item["id"]


def board(url, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOARD_")}
    env[store.DATABASE_URL_ENV] = url
    env["BOARD_ACTOR"] = "worker-1"
    return subprocess.run([sys.executable, "-m", "board.cli", *args],
                          capture_output=True, text=True, env=env, timeout=60)


def imported(tmp_path, root):
    url = f"sqlite:///{tmp_path / 'board.db'}"
    assert board(url, "migrate").returncode == 0
    proc = board(url, "--actor-kind", "system", "import-backlog",
                 "--backlog-py", str(BACKLOG_PY), "--root", str(root))
    assert proc.returncode == 0, proc.stderr
    return url


def board_next(url):
    proc = board(url, "next")
    return proc.returncode, json.loads(proc.stdout)["issue"] and json.loads(proc.stdout)["issue"]["id"]


def assert_parity(tmp_path, root):
    expected = backlog_next(root)
    assert board_next(imported(tmp_path, root)) == expected
    return expected


# --- parity: PRD section 9 criterion 6 ------------------------------------------


def test_parity_first_ready_in_file_order(tmp_path):
    root = write_backlog(
        tmp_path / "backlog",
        brain=[item("BR-1", "vault", "ready")],
        memory_solution=[item("MS-1", "done one", "done"), item("MS-2", "open one", "open"),
                         item("MS-3", "first ready", "ready"), item("MS-4", "later", "ready")],
    )
    assert assert_parity(tmp_path, root) == (0, "MS-3")


def test_parity_in_progress_outranks_an_earlier_ready(tmp_path):
    root = write_backlog(
        tmp_path / "backlog",
        memory_solution=[item("MS-1", "ready first", "ready")],
        brain=[item("BR-1", "checkpointed", "in-progress", "got half way")],
    )
    assert assert_parity(tmp_path, root) == (0, "BR-1")


def test_parity_frozen_and_blocked_are_never_picked(tmp_path):
    root = write_backlog(
        tmp_path / "backlog",
        memory_solution=[item("MS-1", "held", "frozen"), item("MS-2", "stuck", "blocked")],
        packrat_extended=[item("PE-1", "go", "ready")],
    )
    assert assert_parity(tmp_path, root) == (0, "PE-1")


def test_parity_empty_queue_exits_3_on_both(tmp_path):
    root = write_backlog(
        tmp_path / "backlog",
        memory_solution=[item("MS-1", "done", "done"), item("MS-2", "held", "frozen")],
    )
    assert assert_parity(tmp_path, root) == (3, None)


def test_parity_holds_after_an_earlier_open_item_is_made_ready(tmp_path):
    root = write_backlog(
        tmp_path / "backlog",
        memory_solution=[item("MS-3", "not yet", "open"), item("MS-4", "go", "ready")],
    )
    url = imported(tmp_path, root)
    subprocess.run([sys.executable, str(BACKLOG_PY), "set-state", "MS-3", "ready",
                    "--root", str(root)], check=True, capture_output=True, timeout=60)
    moved = board(url, "--actor-kind", "human", "transition", "MS-3", "ready")
    assert moved.returncode == 0, moved.stderr
    assert board_next(url) == backlog_next(root) == (0, "MS-3")


@pytest.mark.skipif(not LIVE_BACKLOG.is_dir(), reason="brain backlog/ NOT FOUND")
def test_parity_on_a_copy_of_the_live_backlog(tmp_path):
    root = tmp_path / "backlog"
    shutil.copytree(LIVE_BACKLOG, root)
    assert_parity(tmp_path, root)


# --- what the import writes -----------------------------------------------------


def test_import_maps_files_items_rank_and_notes(tmp_path):
    root = tmp_path / "backlog"
    root.mkdir()
    (root / "memory-solution.md").write_text(
        "# memory-solution\n\n"
        + item("MS-7", "first", "done", "one", "two")
        + "### MS-9 -- second\n- state: blocked\n- origin: thread 42\n"
        "- notes: wrapped\n  across lines\nfree text\n- notes: last\n"
    )
    url = imported(tmp_path, root)

    first = json.loads(board(url, "show", "MS-7").stdout)
    second = json.loads(board(url, "show", "MS-9").stdout)
    assert (first["project"], first["state"], second["state"]) == ("MS", "done", "need-input")
    assert first["rank"] < second["rank"]
    assert "thread 42" in second["body"]
    notes = [e["note"] for e in second["events"] if e["kind"] == "annotate"]
    assert notes == ["wrapped across lines", "last"]
    assert [e["note"] for e in first["events"] if e["kind"] == "annotate"] == ["one", "two"]

    created = board(url, "--actor-kind", "human", "create", "--project", "MS", "--title", "new")
    assert json.loads(created.stdout)["id"] == "MS-10"


def test_a_notes_line_inside_a_fence_is_not_a_note(tmp_path):
    root = tmp_path / "backlog"
    root.mkdir()
    (root / "brain.md").write_text(
        "### BR-1 -- fenced\n- state: open\n- notes: real one\n"
        "```\n- notes: fenced fake\n```\n- notes: real last\n")
    url = imported(tmp_path, root)
    events = json.loads(board(url, "show", "BR-1").stdout)["events"]
    assert [e["note"] for e in events if e["kind"] == "annotate"] == ["real one", "real last"]


def test_a_second_import_is_refused_and_writes_nothing(tmp_path):
    root = write_backlog(tmp_path / "backlog", memory_solution=[item("MS-1", "a", "ready")])
    url = imported(tmp_path, root)
    write_backlog(root, brain=[item("BR-1", "b", "ready")])

    proc = board(url, "--actor-kind", "system", "import-backlog",
                 "--backlog-py", str(BACKLOG_PY), "--root", str(root))
    assert proc.returncode == 1
    assert "MS" in json.loads(proc.stderr)["message"]
    assert board(url, "show", "BR-1").returncode == 1


def test_a_file_with_two_id_prefixes_is_refused(tmp_path):
    root = write_backlog(tmp_path / "backlog",
                         brain=[item("BR-1", "a", "ready"), item("MS-2", "b", "ready")])
    url = f"sqlite:///{tmp_path / 'board.db'}"
    board(url, "migrate")
    proc = board(url, "--actor-kind", "system", "import-backlog",
                 "--backlog-py", str(BACKLOG_PY), "--root", str(root))
    assert proc.returncode == 1
    assert "brain.md" in json.loads(proc.stderr)["message"]


def test_import_maps_each_backlog_state_to_its_lane(tmp_path):
    lanes = {"open": "backlog", "ready": "ready", "in-progress": "processing",
             "blocked": "need-input", "frozen": "onhold", "done": "done"}
    root = write_backlog(tmp_path / "backlog", memory_solution=[
        item(f"MS-{n}", state, state) for n, state in enumerate(lanes, 1)])
    url = imported(tmp_path, root)
    for n, (state, lane) in enumerate(lanes.items(), 1):
        assert json.loads(board(url, "show", f"MS-{n}").stdout)["state"] == lane, state
