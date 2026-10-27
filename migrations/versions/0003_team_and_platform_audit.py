"""team management: who added a membership and when; platform-level audit trail

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('memberships') as b:
        b.add_column(sa.Column('added_by', sa.String(length=32), nullable=False, server_default=''))
        b.add_column(sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                               server_default=sa.text('CURRENT_TIMESTAMP')))
    op.create_table('platform_audit',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target_user', sa.String(length=32), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_platform_audit_created_at'), 'platform_audit', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_platform_audit_created_at'), table_name='platform_audit')
    op.drop_table('platform_audit')
    with op.batch_alter_table('memberships') as b:
        b.drop_column('created_at')
        b.drop_column('added_by')
