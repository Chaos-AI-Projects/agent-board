"""Engine fixtures: SQLite always, PostgreSQL only when BOARD_TEST_PG_URL is set.

The PostgreSQL leg skips rather than fails when the variable is absent, and
pyproject's `-rs` makes pytest print that skip reason on every run, so a
green run that never touched PostgreSQL says so.
"""

import os

import pytest
from sqlalchemy import inspect, text

from board import store

PG_ENV = "BOARD_TEST_PG_URL"


def _reset_pg(engine):
    # CASCADE, not metadata.drop_all: the workflow <-> issue foreign-key cycle
    # makes drop_all drop the use_alter constraint by name first, which fails
    # on a database a test left at a revision before that constraint existed.
    with engine.begin() as conn:
        for name in inspect(conn).get_table_names():
            conn.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))


@pytest.fixture(params=["sqlite", "postgresql"])
def engine(request, tmp_path):
    if request.param == "sqlite":
        eng = store.make_engine(f"sqlite:///{tmp_path / 'board.db'}")
        yield eng
        eng.dispose()
        return

    url = os.environ.get(PG_ENV)
    if not url:
        pytest.skip(f"PostgreSQL leg NOT RUN: set {PG_ENV} to a throwaway database")
    if url == os.environ.get(store.DATABASE_URL_ENV):
        pytest.fail(f"{PG_ENV} equals {store.DATABASE_URL_ENV}; refusing to drop its tables")
    eng = store.make_engine(url)
    _reset_pg(eng)
    try:
        yield eng
    finally:
        _reset_pg(eng)
        eng.dispose()


@pytest.fixture
def migrated(engine):
    store.upgrade(engine)
    return engine
