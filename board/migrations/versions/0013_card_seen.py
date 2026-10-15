"""Remember when each person last opened each card (AB-67)

`card_seen` holds the newest event id on a card when a person opened it. A
later agent finish makes the card news to them again: highlighted on the
board and counted in the header.

Every card finished before this revision counts as seen, or the first page
load after the upgrade would highlight the board's whole history. So the
upgrade writes a row for each person already on a card, at its newest event.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

EventId = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "card_seen",
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("issue_id", sa.String(32), nullable=False),
        sa.Column("event_id", EventId, nullable=False),
        sa.ForeignKeyConstraint(
            ["issue_id"], ["issue.id"], name=op.f("fk_card_seen_issue_id_issue")
        ),
        sa.PrimaryKeyConstraint("actor", "issue_id", name=op.f("pk_card_seen")),
    )
    op.create_index(op.f("ix_card_seen_issue_id"), "card_seen", ["issue_id"])
    # A person is on a card when they wrote any event on it as a human, or an
    # agent created it for them (AB-63's on_behalf_of).
    op.execute(
        "INSERT INTO card_seen (actor, issue_id, event_id) "
        "SELECT p.who, p.issue_id, (SELECT MAX(e.id) FROM event e WHERE e.issue_id = p.issue_id) "
        "FROM (SELECT actor AS who, issue_id FROM event WHERE actor_kind = 'human' "
        "      UNION "
        "      SELECT on_behalf_of AS who, issue_id FROM event "
        "      WHERE kind = 'create' AND on_behalf_of IS NOT NULL) p"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_card_seen_issue_id"), table_name="card_seen")
    op.drop_table("card_seen")
