"""add_last_modified_to_store_products

Revision ID: b2c3d4e5f6a7
Revises: 8a7004120a5d
Create Date: 2026-08-19 22:05:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = '8a7004120a5d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('store_products', sa.Column('last_modified', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('store_products', 'last_modified')
