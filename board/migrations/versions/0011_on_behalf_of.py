"""Record the person an agent entered a card for (AB-63)

`event.on_behalf_of` holds the creator's email when an agent writes a
card's create event for someone else. Empty means the event's actor is the
creator, as before, so existing rows need no backfill. Plan: brain
`plans/agent-board-card-creator.md` section 2.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("event") as batch:
        batch.add_column(sa.Column("on_behalf_of", sa.String(200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("event") as batch:
        batch.drop_column("on_behalf_of")
