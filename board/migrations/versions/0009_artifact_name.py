"""An optional name on an artifact, shown in place of a url's host (AB-43)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-13
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("artifact") as batch:
        batch.add_column(sa.Column("name", sa.String(200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("artifact") as batch:
        batch.drop_column("name")
