"""event.request_hash, design section 8

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("event", sa.Column("request_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("event", "request_hash")
