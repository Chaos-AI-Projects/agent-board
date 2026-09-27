"""Rename four stored states to the board's lane names (MS-632)

open -> backlog, in-progress -> processing, blocked -> need-input and
frozen -> onhold, on issues and on the from/to states of their events, so
an issue's history reads in the names its lanes use.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

RENAMES = {"open": "backlog", "in-progress": "processing", "blocked": "need-input",
           "frozen": "onhold"}


def _rename(mapping, default):
    for old, new in mapping.items():
        for table, column in (("issue", "state"), ("event", "from_state"),
                              ("event", "to_state")):
            op.execute(sa.text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old")
                       .bindparams(old=old, new=new))
    with op.batch_alter_table("issue") as batch:
        batch.alter_column("state", existing_type=sa.String(32), existing_nullable=False,
                           server_default=default)


def upgrade() -> None:
    _rename(RENAMES, "backlog")


def downgrade() -> None:
    _rename({new: old for old, new in RENAMES.items()}, "open")
