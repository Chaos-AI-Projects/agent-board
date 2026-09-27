"""Alembic environment.

`board.store.upgrade()` passes its engine in through `config.attributes`.
The `alembic` command line has no engine to pass, so it falls back to
BOARD_DATABASE_URL.
"""

from alembic import context

from board import store

config = context.config
given = config.attributes.get("engine")
engine = given or store.make_engine()

with engine.connect() as connection:
    sqlite = connection.dialect.name == "sqlite"
    # A batch migration rebuilds a table, and on SQLite dropping the old
    # `issue` table under foreign_keys=ON fails on every event row pointing
    # at it. SQLite ignores the pragma inside a transaction, so it goes off
    # on the raw connection before one opens, and the keys are checked by
    # hand instead. Alembic commits each SQLite migration by itself, so the
    # outer transaction here is what lets that check roll the whole upgrade
    # back.
    raw = connection.connection.dbapi_connection
    if sqlite:
        raw.execute("PRAGMA foreign_keys=OFF")
    try:
        with connection.begin():
            context.configure(
                connection=connection,
                target_metadata=store.Base.metadata,
                render_as_batch=sqlite,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
            if sqlite:
                broken = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
                if broken:
                    raise RuntimeError(f"migration broke foreign keys: {broken}")
    finally:
        if sqlite:
            raw.execute("PRAGMA foreign_keys=ON")
            if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise RuntimeError("foreign keys stayed off after the migration")

if given is None:
    engine.dispose()
