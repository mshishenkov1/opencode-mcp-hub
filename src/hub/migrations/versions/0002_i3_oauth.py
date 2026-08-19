"""Объекты I-3: oauth_clients, oauth_codes, refresh_tokens, upstream_tokens, sessions, consents
и новые поля connections (R-M2, R-M3).

Revision ID: 0002_i3_oauth
Revises: 0001_i1_base
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_i3_oauth"
down_revision = "0001_i1_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("client_name", sa.String(length=128), nullable=True),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column("grant_types", sa.JSON(), nullable=False),
        sa.Column("response_types", sa.JSON(), nullable=False),
        sa.Column("token_endpoint_auth_method", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_ip", sa.String(length=64), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_oauth_clients_client_id", "oauth_clients", ["client_id"], unique=True)

    op.create_table(
        "oauth_codes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code_sha256", sa.String(length=64), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("alias", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=True),
        sa.Column("redirect_uri", sa.String(length=2048), nullable=False),
        sa.Column("code_challenge", sa.String(length=255), nullable=False),
        sa.Column("code_challenge_method", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.Column("resource", sa.String(length=2048), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_oauth_codes_code_sha256", "oauth_codes", ["code_sha256"], unique=True)
    op.create_index("ix_oauth_codes_client_id", "oauth_codes", ["client_id"], unique=False)

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column("chain_id", sa.String(length=64), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=True),
        sa.Column("alias", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.Column("access_jti", sa.String(length=64), nullable=True),
        sa.Column("access_exp", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_refresh_tokens_token_sha256", "refresh_tokens", ["token_sha256"], unique=True)
    op.create_index("ix_refresh_tokens_chain_id", "refresh_tokens", ["chain_id"], unique=False)

    op.create_table(
        "upstream_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("access_token_enc", sa.String(), nullable=False),
        sa.Column("refresh_token_enc", sa.String(), nullable=True),
        sa.Column("token_type", sa.String(length=32), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("obtained_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("refresh_failed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_upstream_tokens_connection_id", "upstream_tokens", ["connection_id"], unique=True
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_sha256", sa.String(length=64), nullable=False),
        sa.Column("csrf_sha256", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("auth_method", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sessions_session_sha256", "sessions", ["session_sha256"], unique=True)
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)

    op.create_table(
        "consents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("alias", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.Column("preset", sa.String(length=64), nullable=False),
        sa.Column("groups", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "client_id", "alias", name="uq_consents_user_client_alias"),
    )

    with op.batch_alter_table("connections") as batch:
        batch.add_column(sa.Column("needs_reauth_reason", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("last_refresh_at", sa.DateTime(), nullable=True))
        batch.add_column(
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("provider_account", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("connections") as batch:
        batch.drop_column("provider_account")
        batch.drop_column("revision")
        batch.drop_column("last_refresh_at")
        batch.drop_column("needs_reauth_reason")
    op.drop_table("consents")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_index("ix_sessions_session_sha256", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_upstream_tokens_connection_id", table_name="upstream_tokens")
    op.drop_table("upstream_tokens")
    op.drop_index("ix_refresh_tokens_chain_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_token_sha256", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("ix_oauth_codes_client_id", table_name="oauth_codes")
    op.drop_index("ix_oauth_codes_code_sha256", table_name="oauth_codes")
    op.drop_table("oauth_codes")
    op.drop_index("ix_oauth_clients_client_id", table_name="oauth_clients")
    op.drop_table("oauth_clients")
