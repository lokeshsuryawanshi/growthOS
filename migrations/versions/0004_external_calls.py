"""external_calls: cost ledger for search credits, contact lookups and sends

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('external_calls',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('run_id', sa.String(length=32), nullable=True),
    sa.Column('node_key', sa.String(length=64), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('operation', sa.String(length=32), nullable=False),
    sa.Column('units', sa.Float(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_external_calls_created_at'), 'external_calls', ['created_at'], unique=False)
    op.create_index(op.f('ix_external_calls_run_id'), 'external_calls', ['run_id'], unique=False)
    op.create_index(op.f('ix_external_calls_tenant_id'), 'external_calls', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_external_calls_tenant_id'), table_name='external_calls')
    op.drop_index(op.f('ix_external_calls_run_id'), table_name='external_calls')
    op.drop_index(op.f('ix_external_calls_created_at'), table_name='external_calls')
    op.drop_table('external_calls')
