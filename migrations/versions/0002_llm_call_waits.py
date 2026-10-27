"""llm_calls: record time spent waiting (pacing, 429 backoff) separately from provider latency

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('llm_calls') as b:
        b.add_column(sa.Column('pace_wait_ms', sa.Integer(), nullable=False, server_default='0'))
        b.add_column(sa.Column('retry_wait_ms', sa.Integer(), nullable=False, server_default='0'))
        b.add_column(sa.Column('route_note', sa.String(length=300), nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('llm_calls') as b:
        b.drop_column('route_note')
        b.drop_column('retry_wait_ms')
        b.drop_column('pace_wait_ms')
