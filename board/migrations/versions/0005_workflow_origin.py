"""A workflow can be the plan for one issue (MS-644)

`workflow.origin_issue_id` names the issue an agent or human broke into
steps. It is unique: one issue has at most one plan.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workflow") as batch:
        batch.add_column(sa.Column("origin_issue_id", sa.String(32), nullable=True))
        batch.create_unique_constraint(op.f("uq_workflow_origin_issue_id"),
                                       ["origin_issue_id"])
        batch.create_foreign_key(op.f("fk_workflow_origin_issue_id_issue"), "issue",
                                 ["origin_issue_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("workflow") as batch:
        batch.drop_constraint(op.f("fk_workflow_origin_issue_id_issue"), type_="foreignkey")
        batch.drop_constraint(op.f("uq_workflow_origin_issue_id"), type_="unique")
        batch.drop_column("origin_issue_id")
