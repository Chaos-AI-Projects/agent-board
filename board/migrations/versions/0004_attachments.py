"""Files attached to an issue or to one of its notes (MS-643)

Only metadata lives here. The bytes sit on disk under BOARD_ATTACHMENT_DIR,
named by their SHA-256. `event_id` is set when the file came with a note, so
the history can show it under that note.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

Timestamp = sa.DateTime(timezone=True)
EventId = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "attachment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("issue_id", sa.String(32), nullable=False),
        sa.Column("event_id", EventId, nullable=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(200), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("added_at", Timestamp, nullable=False),
        sa.Column("added_by", sa.String(200), nullable=False),
        sa.ForeignKeyConstraint(
            ["issue_id"], ["issue.id"], name=op.f("fk_attachment_issue_id_issue")
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["event.id"], name=op.f("fk_attachment_event_id_event")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attachment")),
    )
    op.create_index(op.f("ix_attachment_issue_id"), "attachment", ["issue_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_attachment_issue_id"), table_name="attachment")
    op.drop_table("attachment")
