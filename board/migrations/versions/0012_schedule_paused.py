"""A card's schedule can be switched off and on (AB-65)

`issue.schedule_paused` is true while the switch is off: the cron and zone
stay, `next_run_at` is empty and `next` never claims the card. Existing rows
are on, so the server default is false.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("issue") as batch:
        batch.add_column(sa.Column("schedule_paused", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("issue") as batch:
        batch.drop_column("schedule_paused")
