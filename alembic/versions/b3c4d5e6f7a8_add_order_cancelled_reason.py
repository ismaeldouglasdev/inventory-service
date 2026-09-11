"""add order cancelled_reason

Revision ID: b3c4d5e6f7a8
Revises: fee1f1952c89
Create Date: 2026-09-10 11:40:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, None] = 'fee1f1952c89'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # B3: registrar motivo do cancelamento (estoque insuficiente no confirm,
    # cancelamento manual do lojista). Nullable — só preenchido quando cancelado.
    op.add_column('orders', sa.Column('cancelled_reason', sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column('orders', 'cancelled_reason')