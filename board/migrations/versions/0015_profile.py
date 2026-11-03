"""A short profile per person, set by them or over MCP (AB-89)

`profile` holds one row per actor who has written one. Nobody has one yet,
so the upgrade creates the table and writes nothing.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-04
"""

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

Timestamp = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "profile",
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("updated_at", Timestamp, nullable=False),
        sa.PrimaryKeyConstraint("actor", name=op.f("pk_profile")),
    )


def downgrade() -> None:
    op.drop_table("profile")
