"""OAuth clients, codes and tokens for remote MCP (MS-649)

Codes and tokens are stored as SHA-256 hex only. A confidential client's own
secret sits in oauth_client.info as the SDK serialises it; public clients,
which is what claude.ai registers as, have none.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-29
"""

from alembic import op
import sqlalchemy as sa

Timestamp = sa.DateTime(timezone=True)

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_client",
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("info", sa.Text(), nullable=False),
        sa.Column("created_at", Timestamp, nullable=False),
        sa.PrimaryKeyConstraint("client_id", name=op.f("pk_oauth_client")),
    )
    op.create_table(
        "oauth_code",
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("email", sa.String(200), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("redirect_uri_explicit", sa.Boolean(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("expires_at", Timestamp, nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"], ["oauth_client.client_id"],
            name=op.f("fk_oauth_code_client_id_oauth_client"),
        ),
        sa.PrimaryKeyConstraint("code_hash", name=op.f("pk_oauth_code")),
    )
    op.create_index(op.f("ix_oauth_code_client_id"), "oauth_code", ["client_id"])
    op.create_table(
        "oauth_token",
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("grant_id", sa.String(64), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("email", sa.String(200), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("expires_at", Timestamp, nullable=False),
        sa.Column("revoked_at", Timestamp, nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"], ["oauth_client.client_id"],
            name=op.f("fk_oauth_token_client_id_oauth_client"),
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_oauth_token")),
    )
    op.create_index(op.f("ix_oauth_token_grant_id"), "oauth_token", ["grant_id"])
    op.create_index(op.f("ix_oauth_token_client_id"), "oauth_token", ["client_id"])
    op.create_index(op.f("ix_oauth_token_email"), "oauth_token", ["email"])


def downgrade() -> None:
    op.drop_index(op.f("ix_oauth_token_email"), table_name="oauth_token")
    op.drop_index(op.f("ix_oauth_token_client_id"), table_name="oauth_token")
    op.drop_index(op.f("ix_oauth_token_grant_id"), table_name="oauth_token")
    op.drop_table("oauth_token")
    op.drop_index(op.f("ix_oauth_code_client_id"), table_name="oauth_code")
    op.drop_table("oauth_code")
    op.drop_table("oauth_client")
