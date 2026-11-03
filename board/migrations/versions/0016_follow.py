"""People follow each other (AB-90)

`follow` holds one row per person following another. Nobody follows anyone
yet, so the upgrade creates the table and writes nothing.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-04
"""

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

Timestamp = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "follow",
        sa.Column("follower", sa.String(200), nullable=False),
        sa.Column("followee", sa.String(200), nullable=False),
        sa.Column("created_at", Timestamp, nullable=False),
        sa.PrimaryKeyConstraint("follower", "followee", name=op.f("pk_follow")),
    )


def downgrade() -> None:
    op.drop_table("follow")
