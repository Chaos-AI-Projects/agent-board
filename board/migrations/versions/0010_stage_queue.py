"""Split a card's state into a stage and a queue status (AB-52)

`issue.state` keeps its column and now holds the stage: backlog, planning,
implement, done or cancelled. The new `issue.queue` holds ready, processing
or on-hold, or null when the card is not queued. `schedule` and
`next_run_at` are for cron-scheduled cards. Events gain `from_queue` and
`to_queue` and their old states are rewritten with the same mapping, as
0003 did, so history reads in the new names. Plan: brain
`plans/agent-board-lanes.md` section 3.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-14
"""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

Timestamp = sa.DateTime(timezone=True)

# Old state -> (stage, queue status).
SPLIT = {
    "backlog": ("backlog", None),
    "ready": ("implement", "ready"),
    "processing": ("implement", "processing"),
    "need-input": ("implement", None),
    "onhold": ("implement", "on-hold"),
    "done": ("done", None),
    "cancelled": ("cancelled", None),
}

# A queued planning or implement card folds back by its queue status alone.
FOLD = {"ready": "ready", "processing": "processing", "on-hold": "onhold", None: "need-input"}

COLUMNS = (("issue", "state", "queue"), ("event", "from_state", "from_queue"),
           ("event", "to_state", "to_queue"))


def upgrade() -> None:
    with op.batch_alter_table("issue") as batch:
        batch.add_column(sa.Column("queue", sa.String(32), nullable=True))
        batch.add_column(sa.Column("schedule", sa.String(200), nullable=True))
        batch.add_column(sa.Column("schedule_tz", sa.String(64), nullable=True))
        batch.add_column(sa.Column("next_run_at", Timestamp, nullable=True))
        batch.create_index("ix_issue_queue_rank", ["queue", "rank"])
    with op.batch_alter_table("event") as batch:
        batch.add_column(sa.Column("from_queue", sa.String(32), nullable=True))
        batch.add_column(sa.Column("to_queue", sa.String(32), nullable=True))
    for table, state, queue in COLUMNS:
        for old, (stage, q) in SPLIT.items():
            op.execute(sa.text(f"UPDATE {table} SET {state} = :stage, {queue} = :q "
                               f"WHERE {state} = :old")
                       .bindparams(stage=stage, q=q, old=old))


def downgrade() -> None:
    for table, state, queue in COLUMNS:
        for q, old in FOLD.items():
            where = f"{queue} IS NULL" if q is None else f"{queue} = :q"
            stmt = sa.text(f"UPDATE {table} SET {state} = :old "
                           f"WHERE {state} IN ('planning', 'implement') AND {where}")
            params = {"old": old} if q is None else {"old": old, "q": q}
            op.execute(stmt.bindparams(**params))
    with op.batch_alter_table("event") as batch:
        batch.drop_column("to_queue")
        batch.drop_column("from_queue")
    with op.batch_alter_table("issue") as batch:
        batch.drop_index("ix_issue_queue_rank")
        batch.drop_column("next_run_at")
        batch.drop_column("schedule_tz")
        batch.drop_column("schedule")
        batch.drop_column("queue")
