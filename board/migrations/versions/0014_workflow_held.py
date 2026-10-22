"""A plan's workflow can be held by its parent's id (AB-83)

`workflow.held` is true while `next` hands out none of the workflow's
steps. A step already claimed keeps its lease. Existing workflows are not
held, so the server default is false.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-23
"""

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workflow") as batch:
        batch.add_column(sa.Column("held", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("workflow") as batch:
        batch.drop_column("held")
