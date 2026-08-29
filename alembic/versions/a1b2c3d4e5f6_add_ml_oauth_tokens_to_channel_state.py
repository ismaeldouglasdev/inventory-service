"""add_ml_oauth_tokens_to_channel_state

Revision ID: a1b2c3d4e5f6
Revises: b2c3d4e5f6a7
Create Date: 2026-08-29 13:10:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "channel_state",
        sa.Column("access_token", sa.Text(), nullable=True),
    )
    op.add_column(
        "channel_state",
        sa.Column("refresh_token", sa.Text(), nullable=True),
    )
    op.add_column(
        "channel_state",
        sa.Column("ml_user_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "channel_state",
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channel_state", "token_expires_at")
    op.drop_column("channel_state", "ml_user_id")
    op.drop_column("channel_state", "refresh_token")
    op.drop_column("channel_state", "access_token")
