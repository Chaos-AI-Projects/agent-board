import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import bindparam, inspect, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from board import store

TABLES = {
    "project",
    "issue",
    "event",
    "artifact",
    "attachment",
    "label",
    "workflow",
    "template",
    "template_step",
}


def now():
    return datetime.now(timezone.utc)


# --- database URL -----------------------------------------------------------


def test_make_engine_without_url_names_the_variable(monkeypatch):
    monkeypatch.delenv(store.DATABASE_URL_ENV, raising=False)
    with pytest.raises(store.ConfigError, match="BOARD_DATABASE_URL"):
        store.make_engine()


def test_make_engine_reads_the_variable(monkeypatch, tmp_path):
    path = tmp_path / "env.db"
    monkeypatch.setenv(store.DATABASE_URL_ENV, f"sqlite:///{path}")
    engine = store.make_engine()
    assert engine.url.database == str(path)


@pytest.mark.parametrize(
    "given",
    ["postgresql://u:p@db.example/board", "postgres://u:p@db.example/board"],
)
def test_bare_postgres_url_gets_the_installed_driver(given):
    url = store.normalize_url(given)
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "db.example"
    assert url.database == "board"


def test_explicit_driver_is_left_alone():
    url = store.normalize_url("postgresql+psycopg2://u:p@h/board")
    assert url.drivername == "postgresql+psycopg2"


# --- migrations -------------------------------------------------------------


def test_upgrade_creates_every_table(migrated):
    names = set(inspect(migrated).get_table_names())
    assert names == TABLES | {"alembic_version"}


def test_migration_matches_the_models(migrated):
    with migrated.connect() as conn:
        # SQLite reflects a '' default in a form alembic misreads, so server
        # defaults are compared on PostgreSQL only.
        opts = {
            "compare_type": True,
            "compare_server_default": migrated.dialect.name == "postgresql",
        }
        ctx = MigrationContext.configure(conn, opts=opts)
        diff = compare_metadata(ctx, store.Base.metadata)
    assert diff == []


def test_upgrade_is_idempotent(migrated):
    store.upgrade(migrated)
    assert set(inspect(migrated).get_table_names()) == TABLES | {"alembic_version"}


def test_downgrade_removes_every_table(migrated):
    store.downgrade(migrated)
    assert set(inspect(migrated).get_table_names()) <= {"alembic_version"}


OLD_STATES = ["open", "ready", "in-progress", "blocked", "frozen", "done", "cancelled"]
NEW_STATES = ["backlog", "ready", "processing", "need-input", "onhold", "done", "cancelled"]


def _stored_states(engine):
    with engine.connect() as conn:
        issues = conn.execute(text("SELECT state FROM issue ORDER BY rank")).scalars().all()
        events = conn.execute(text(
            "SELECT from_state, to_state FROM event ORDER BY id")).all()
    return issues, [tuple(e) for e in events]


def test_0003_renames_stored_states_and_downgrade_restores_them(engine):
    store.upgrade(engine, "0002")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO project (key, name, next_number) VALUES ('MS', 'm', 1)"))
        for n, state in enumerate(OLD_STATES):
            conn.execute(text(
                "INSERT INTO issue (id, project_key, title, body, state, rank, version, "
                "created_at, updated_at) VALUES (:id, 'MS', 't', '', :state, :rank, 1, "
                ":now, :now)"), {"id": f"MS-{n}", "state": state, "rank": n, "now": now()})
            conn.execute(text(
                "INSERT INTO event (issue_id, at, actor, actor_kind, kind, from_state, to_state) "
                "VALUES (:id, :now, 'a', 'human', 'transition', :state, :state)"),
                {"id": f"MS-{n}", "state": state, "now": now()})
        conn.execute(text(
            "INSERT INTO event (issue_id, at, actor, actor_kind, kind) "
            "VALUES ('MS-0', :now, 'a', 'human', 'annotate')"), {"now": now()})

    store.upgrade(engine)
    assert _stored_states(engine) == (NEW_STATES, [(s, s) for s in NEW_STATES] + [(None, None)])

    store.downgrade(engine, "0002")
    assert _stored_states(engine) == (OLD_STATES, [(s, s) for s in OLD_STATES] + [(None, None)])


def test_a_migration_that_breaks_a_foreign_key_commits_nothing(engine):
    if engine.dialect.name != "sqlite":
        pytest.skip("the foreign-key guard in env.py is SQLite's")
    store.upgrade(engine, "0002")
    raw = sqlite3.connect(engine.url.database)  # foreign keys off, as sqlite3 opens
    raw.execute("INSERT INTO event (issue_id, at, actor, actor_kind, kind) "
                "VALUES ('MS-404', '2026-09-27', 'a', 'human', 'annotate')")
    raw.commit()
    raw.close()

    with pytest.raises(RuntimeError, match="foreign keys"):
        store.upgrade(engine)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0002"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


# --- schema behaviour -------------------------------------------------------


def _project_and_issue(session, issue_id="MS-1", **issue_kw):
    if session.get(store.Project, "MS") is None:
        session.add(store.Project(key="MS", name="memory-solution"))
        session.flush()
    issue = store.Issue(
        id=issue_id,
        project_key="MS",
        title="t",
        rank=issue_kw.pop("rank", 1),
        created_at=now(),
        updated_at=now(),
        **issue_kw,
    )
    session.add(issue)
    session.flush()
    return issue


def test_issue_defaults(migrated):
    with Session(migrated) as s:
        issue = _project_and_issue(s)
        s.commit()
        s.refresh(issue)
        assert issue.state == "backlog"
        assert issue.version == 1
        assert issue.body == ""
        assert issue.lease_holder is None
        assert issue.lease_expires_at is None
        assert issue.project.next_number == 1


def test_foreign_keys_are_enforced(migrated):
    with Session(migrated) as s:
        s.add(
            store.Issue(
                id="XX-1",
                project_key="XX",
                title="orphan",
                rank=1,
                created_at=now(),
                updated_at=now(),
            )
        )
        with pytest.raises(IntegrityError):
            s.flush()


def test_children_round_trip(migrated):
    with Session(migrated) as s:
        issue = _project_and_issue(s)
        issue.labels.append(store.Label(name="board"))
        issue.artifacts.append(
            store.Artifact(kind="commit", ref="abc123", closes=True, added_at=now(), added_by="a")
        )
        issue.events.append(
            store.Event(at=now(), actor="a", actor_kind="agent", kind="create")
        )
        s.commit()

    with Session(migrated) as s:
        issue = s.get(store.Issue, "MS-1")
        assert [l.name for l in issue.labels] == ["board"]
        assert issue.artifacts[0].closes is True
        assert issue.events[0].kind == "create"
        assert isinstance(issue.events[0].id, int)


def test_idempotency_key_is_unique(migrated):
    with Session(migrated) as s:
        issue = _project_and_issue(s)
        for _ in range(2):
            issue.events.append(
                store.Event(
                    at=now(), actor="a", actor_kind="agent", kind="annotate",
                    idempotency_key="k1",
                )
            )
        with pytest.raises(IntegrityError):
            s.flush()


def test_events_without_a_key_do_not_collide(migrated):
    with Session(migrated) as s:
        issue = _project_and_issue(s)
        for _ in range(2):
            issue.events.append(
                store.Event(at=now(), actor="a", actor_kind="agent", kind="annotate")
            )
        s.commit()
        assert len(issue.events) == 2


def test_one_issue_per_workflow_step(migrated):
    with Session(migrated) as s:
        wf = store.Workflow(title="w", created_at=now())
        s.add(wf)
        s.flush()
        _project_and_issue(s, "MS-1", workflow_id=wf.id, position=1)
        with pytest.raises(IntegrityError):
            _project_and_issue(s, "MS-2", workflow_id=wf.id, position=1)


def test_template_steps_are_ordered(migrated):
    with Session(migrated) as s:
        tpl = store.Template(name="release", title="Release")
        tpl.steps.append(store.TemplateStep(position=2, title="ship", body=""))
        tpl.steps.append(store.TemplateStep(position=1, title="build", body=""))
        s.add(tpl)
        s.commit()
        tpl_id = tpl.id

    with Session(migrated) as s:
        tpl = s.get(store.Template, tpl_id)
        assert [st.title for st in tpl.steps] == ["build", "ship"]


# --- timestamps -------------------------------------------------------------


def test_timestamps_round_trip_as_utc(migrated):
    # SQLite has no timezone type, so without a decorator the offset is dropped.
    expiry = datetime(2026, 9, 25, 20, 0, tzinfo=timezone(timedelta(hours=10)))
    with Session(migrated) as s:
        _project_and_issue(s, lease_expires_at=expiry)
        s.commit()

    with Session(migrated) as s:
        got = s.get(store.Issue, "MS-1").lease_expires_at
        assert got.utcoffset() == timedelta(0)
        assert got == datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
        assert got < now()  # aware vs aware: comparable


def test_naive_timestamps_are_refused(migrated):
    with Session(migrated) as s:
        with pytest.raises(StatementError, match="timezone"):
            _project_and_issue(s, lease_expires_at=datetime(2026, 9, 25, 20, 0))


def test_server_defaults_apply_to_a_raw_insert(migrated):
    stamp = now()
    with migrated.begin() as conn:
        conn.execute(text("INSERT INTO project (key, name) VALUES ('MS', 'm')"))
        conn.execute(
            text(
                "INSERT INTO issue (id, project_key, title, rank, created_at, updated_at)"
                " VALUES ('MS-1', 'MS', 't', 1, :at, :at)"
            ).bindparams(bindparam("at", type_=store.Timestamp())),
            {"at": stamp},
        )
        row = conn.execute(
            text("SELECT state, version, body FROM issue WHERE id = 'MS-1'")
        ).one()
        nxt = conn.execute(text("SELECT next_number FROM project")).scalar_one()
    assert tuple(row) == ("backlog", 1, "")
    assert nxt == 1


# --- SQLite as the production engine (MS-626) -------------------------------------


def _sqlite(tmp_path):
    return store.make_engine(f"sqlite:///{tmp_path / 'shared.db'}")


def test_sqlite_runs_in_wal_mode_with_a_long_busy_timeout(tmp_path):
    # The busy timeout makes a second connection wait for the lock instead of
    # failing "database is locked".
    eng = _sqlite(tmp_path)
    with eng.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() >= store.SQLITE_BUSY_TIMEOUT_MS
    assert store.SQLITE_BUSY_TIMEOUT_MS >= 30_000
    eng.dispose()


def test_two_engines_on_one_file_wait_for_each_other(tmp_path):
    import threading
    import time

    from board import core

    web_side, agent_side = _sqlite(tmp_path), _sqlite(tmp_path)
    store.upgrade(web_side)
    core.create_project(web_side, "MS", "memory-solution")
    core.create(web_side, "MS", "t", actor="chaos", actor_kind="human", state="ready")

    held = threading.Event()

    def hold_write_lock():
        with web_side.begin() as conn:
            conn.exec_driver_sql("UPDATE issue SET title = 'held' WHERE 1 = 0")
            held.set()
            time.sleep(0.5)

    t = threading.Thread(target=hold_write_lock)
    t.start()
    assert held.wait(5), "the holder never took the write lock"
    # Every SQLAlchemy transaction opens BEGIN IMMEDIATE, reads included, so a
    # read queues behind the writer too. The busy timeout is what lets it wait.
    with agent_side.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM issue").scalar() == 1
    claimed = core.next(agent_side, worker="agent")  # a write waits, then succeeds
    t.join()
    assert claimed is not None
    web_side.dispose()
    agent_side.dispose()


def test_0004_adds_the_attachment_table_and_downgrade_drops_only_it(engine):
    store.upgrade(engine, "0004")
    assert "attachment" in inspect(engine).get_table_names()
    store.downgrade(engine, "0003")
    names = set(inspect(engine).get_table_names())
    assert "attachment" not in names and TABLES - {"attachment"} <= names


def test_0005_adds_workflow_origin_and_downgrade_drops_only_it(engine):
    store.upgrade(engine, "0005")
    cols = {c["name"] for c in inspect(engine).get_columns("workflow")}
    assert "origin_issue_id" in cols
    store.downgrade(engine, "0004")
    cols = {c["name"] for c in inspect(engine).get_columns("workflow")}
    assert "origin_issue_id" not in cols
    assert "attachment" in inspect(engine).get_table_names()
