"""Dependencies between issues (MS-646)

One row says `issue_id` waits until `depends_on_id` is done. Plain rows in
both engines; `next` reads them with a NOT EXISTS.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29
"""

from alembic import op
import sqlalchemy as sa

Timestamp = sa.DateTime(timezone=True)

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dependency",
        sa.Column("issue_id", sa.String(32), nullable=False),
        sa.Column("depends_on_id", sa.String(32), nullable=False),
        sa.Column("created_at", Timestamp, nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.ForeignKeyConstraint(
            ["issue_id"], ["issue.id"], name=op.f("fk_dependency_issue_id_issue")
        ),
        sa.ForeignKeyConstraint(
            ["depends_on_id"], ["issue.id"], name=op.f("fk_dependency_depends_on_id_issue")
        ),
        sa.PrimaryKeyConstraint("issue_id", "depends_on_id", name=op.f("pk_dependency")),
    )
    op.create_index(op.f("ix_dependency_depends_on_id"), "dependency", ["depends_on_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_dependency_depends_on_id"), table_name="dependency")
    op.drop_table("dependency")
