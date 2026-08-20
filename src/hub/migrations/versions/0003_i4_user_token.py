"""Способ подключения в connections: колонка auth_method (R-M1/R-M3, R-U4, решение 70).

Revision ID: 0003_i4_user_token
Revises: 0002_i3_oauth
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_i4_user_token"
down_revision = "0002_i3_oauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("connections") as batch:
        batch.add_column(sa.Column("auth_method", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("connections") as batch:
        batch.drop_column("auth_method")
