"""Обмен присланного токена на постоянный: происхождение токена в upstream_tokens (R-U17.4).

Существующие строки получают ``token_origin = 'submitted'`` и NULL в остальных колонках —
то есть прежнее поведение: предупреждения нет, отзыв при отключении не выполняется (R-U14, R-U15.4).

Revision ID: 0004_i4_token_exchange
Revises: 0003_i4_user_token
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_i4_token_exchange"
down_revision = "0003_i4_user_token"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("upstream_tokens") as batch:
        batch.add_column(sa.Column("issued_token_id", sa.String(length=255), nullable=True))
        batch.add_column(
            sa.Column(
                "token_origin",
                sa.String(length=16),
                nullable=False,
                server_default="submitted",
            )
        )
        batch.add_column(sa.Column("token_origin_reason", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("submitted_expires_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("upstream_tokens") as batch:
        batch.drop_column("submitted_expires_at")
        batch.drop_column("token_origin_reason")
        batch.drop_column("token_origin")
        batch.drop_column("issued_token_id")
