"""initial schema, design section 2

Revision ID: 0001
Revises:
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

Timestamp = sa.DateTime(timezone=True)
EventId = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "project",
        sa.Column("key", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("next_number", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_project")),
    )
    op.create_table(
        "template",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_template")),
        sa.UniqueConstraint("name", name=op.f("uq_template_name")),
    )
    op.create_table(
        "template_step",
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id"], ["template.id"],
            name=op.f("fk_template_step_template_id_template"),
        ),
        sa.PrimaryKeyConstraint("template_id", "position", name=op.f("pk_template_step")),
    )
    op.create_table(
        "workflow",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=True),
        sa.Column("created_at", Timestamp, nullable=False),
        sa.Column("archived_at", Timestamp, nullable=True),
        sa.ForeignKeyConstraint(
            ["template_id"], ["template.id"],
            name=op.f("fk_workflow_template_id_template"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow")),
    )
    op.create_table(
        "issue",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("project_key", sa.String(16), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("state", sa.String(32), server_default="open", nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("assignee", sa.String(200), nullable=True),
        sa.Column("workflow_id", sa.Integer(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("lease_holder", sa.String(200), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", Timestamp, nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", Timestamp, nullable=False),
        sa.Column("updated_at", Timestamp, nullable=False),
        sa.ForeignKeyConstraint(
            ["project_key"], ["project.key"], name=op.f("fk_issue_project_key_project")
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflow.id"], name=op.f("fk_issue_workflow_id_workflow")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_issue")),
        sa.UniqueConstraint(
            "workflow_id", "position", name=op.f("uq_issue_workflow_id_position")
        ),
    )
    op.create_index(op.f("ix_issue_project_key"), "issue", ["project_key"])
    op.create_index("ix_issue_state_rank", "issue", ["state", "rank"])
    op.create_table(
        "event",
        sa.Column("id", EventId, autoincrement=True, nullable=False),
        sa.Column("issue_id", sa.String(32), nullable=False),
        sa.Column("at", Timestamp, nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("from_state", sa.String(32), nullable=True),
        sa.Column("to_state", sa.String(32), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=True),
        sa.ForeignKeyConstraint(
            ["issue_id"], ["issue.id"], name=op.f("fk_event_issue_id_issue")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_event_idempotency_key")),
    )
    op.create_index(op.f("ix_event_issue_id"), "event", ["issue_id"])
    op.create_table(
        "artifact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("issue_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("ref", sa.String(1000), nullable=False),
        sa.Column("closes", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("added_at", Timestamp, nullable=False),
        sa.Column("added_by", sa.String(200), nullable=False),
        sa.ForeignKeyConstraint(
            ["issue_id"], ["issue.id"], name=op.f("fk_artifact_issue_id_issue")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifact")),
    )
    op.create_index(op.f("ix_artifact_issue_id"), "artifact", ["issue_id"])
    op.create_table(
        "label",
        sa.Column("issue_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.ForeignKeyConstraint(
            ["issue_id"], ["issue.id"], name=op.f("fk_label_issue_id_issue")
        ),
        sa.PrimaryKeyConstraint("issue_id", "name", name=op.f("pk_label")),
    )


def downgrade() -> None:
    op.drop_table("label")
    op.drop_index(op.f("ix_artifact_issue_id"), table_name="artifact")
    op.drop_table("artifact")
    op.drop_index(op.f("ix_event_issue_id"), table_name="event")
    op.drop_table("event")
    op.drop_index("ix_issue_state_rank", table_name="issue")
    op.drop_index(op.f("ix_issue_project_key"), table_name="issue")
    op.drop_table("issue")
    op.drop_table("workflow")
    op.drop_table("template_step")
    op.drop_table("template")
    op.drop_table("project")
