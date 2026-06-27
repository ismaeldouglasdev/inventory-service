"""create_event_store_archive

Revision ID: 8a7004120a5d
Revises: 970ef416daed
Create Date: 2026-06-27 16:36:01.453704
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8a7004120a5d'
down_revision: Union[str, None] = '970ef416daed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('event_store_archive',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('sku', sa.String(length=64), nullable=True),
        sa.Column('channel', sa.String(length=32), nullable=True),
        sa.Column('ospos_synced', sa.Boolean(), default=False),
        sa.Column('retry_count', sa.Integer(), default=0),
        sa.Column('max_retries', sa.Integer(), default=5),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('archived_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_archive_sku', 'event_store_archive', ['sku'])
    op.create_index('ix_archive_state', 'event_store_archive', ['state'])
    op.create_index('ix_archive_created_at', 'event_store_archive', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_archive_created_at', table_name='event_store_archive')
    op.drop_index('ix_archive_state', table_name='event_store_archive')
    op.drop_index('ix_archive_sku', table_name='event_store_archive')
    op.drop_table('event_store_archive')
