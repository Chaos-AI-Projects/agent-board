"""A stored colour bucket per project (MS-655)

Existing projects are numbered 0, 1, 2, ... in creation order. SQLite has that
order as rowid. PostgreSQL has no stable equivalent, so there it is the order
of each project's first issue, then key.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-30
"""

import hashlib

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch:
        batch.add_column(sa.Column("colour_bucket", sa.Integer(), nullable=True))
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        order = "SELECT key FROM project ORDER BY rowid"
    else:
        order = ("SELECT p.key FROM project p LEFT JOIN issue i ON i.project_key = p.key "
                 "GROUP BY p.key ORDER BY MIN(i.created_at) NULLS LAST, p.key")
    keys = conn.execute(sa.text(order)).scalars().all()
    # The first ten take one bucket each; any beyond share by key hash, as
    # core.create_project would have placed them. Inlined so a later change to
    # core cannot rewrite what this revision did.
    for n, key in enumerate(keys):
        bucket = n if n < 10 else int.from_bytes(
            hashlib.sha256(key.encode()).digest()[:4], "big") % 10
        conn.execute(sa.text("UPDATE project SET colour_bucket = :b WHERE key = :k"),
                     {"b": bucket, "k": key})


def downgrade() -> None:
    with op.batch_alter_table("project") as batch:
        batch.drop_column("colour_bucket")
