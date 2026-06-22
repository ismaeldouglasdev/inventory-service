"""create_store_products

Revision ID: e7f1d57c0207
Revises: e587fac1efa1
Create Date: 2026-06-22 20:17:52.713304
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7f1d57c0207'
down_revision: Union[str, None] = 'e587fac1efa1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('store_products',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('ospos_id', sa.Integer(), nullable=False),
    sa.Column('sku', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('price', sa.Float(), nullable=False),
    sa.Column('category', sa.String(length=128), nullable=False),
    sa.Column('stock', sa.Integer(), nullable=False),
    sa.Column('image_url', sa.String(length=512), nullable=True),
    sa.Column('store_visible', sa.Boolean(), nullable=False),
    sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('ospos_id')
    )
    op.create_index(op.f('ix_store_products_category'), 'store_products', ['category'], unique=False)
    op.create_index(op.f('ix_store_products_sku'), 'store_products', ['sku'], unique=False)
    op.create_index(op.f('ix_store_products_stock'), 'store_products', ['stock'], unique=False)
    op.create_index(op.f('ix_store_products_store_visible'), 'store_products', ['store_visible'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_store_products_store_visible'), table_name='store_products')
    op.drop_index(op.f('ix_store_products_stock'), table_name='store_products')
    op.drop_index(op.f('ix_store_products_sku'), table_name='store_products')
    op.drop_index(op.f('ix_store_products_category'), table_name='store_products')
    op.drop_table('store_products')
