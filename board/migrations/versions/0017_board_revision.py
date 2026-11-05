"""A revision counter every write moves (AB-100)

`board_revision` holds one row. A commit that wrote anything adds one to its
counter, so a stamp poll reads one row to learn whether the board changed.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "board_revision",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("n", sa.BigInteger, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_board_revision")),
    )
    op.bulk_insert(table, [{"id": 1, "n": 0}])


def downgrade() -> None:
    op.drop_table("board_revision")
