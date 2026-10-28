"""v2: one operator account (Google sign-in) owns its client profiles and its own API keys; no roles, teams, admin or
Gmail sending.

v1 accounts signed in with a password, which v2 no longer has, so they could never sign in again. Their rows are
removed rather than left orphaned (v1 data lived on ephemeral storage and was disposable). On an empty database this
migration only reshapes the tables.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-29
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

# Children before parents (foreign keys).
V1_TENANT_TABLES = ['outreach_messages', 'lead_rejections', 'leads', 'posts', 'calendar_slots', 'run_nodes', 'runs',
                    'llm_calls', 'external_calls', 'llm_cache', 'trace_events', 'artifacts', 'knowledge_docs',
                    'client_profiles', 'suppression_entries', 'feedback', 'guardrail_incidents', 'audit_log',
                    'integration_credentials']


def upgrade() -> None:
    for t in V1_TENANT_TABLES:
        op.execute(sa.text(f'DELETE FROM {t}'))
    op.drop_table('integration_credentials')
    op.drop_index('ix_platform_audit_created_at', table_name='platform_audit')
    op.drop_table('platform_audit')
    with op.batch_alter_table('memberships') as b:
        b.drop_index('ix_memberships_tenant_id')
        b.drop_index('ix_memberships_user_id')
    op.drop_table('memberships')
    op.execute(sa.text('DELETE FROM tenants'))
    op.drop_table('users')
    op.create_table('users',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('google_sub', sa.String(length=64), nullable=False),
        sa.Column('email', sa.String(length=200), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('monthly_cap_usd', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('google_sub'))
    with op.batch_alter_table('tenants') as b:
        b.add_column(sa.Column('owner_user_id', sa.String(length=32), nullable=False))
        b.create_index('ix_tenants_owner_user_id', ['owner_user_id'], unique=False)
        b.create_foreign_key('fk_tenants_owner_user_id_users', 'users', ['owner_user_id'], ['id'])
        b.drop_column('kill_switch')
        b.drop_column('monthly_budget_usd')
    with op.batch_alter_table('outreach_messages') as b:
        b.drop_column('external_id')
        b.drop_column('send_error')
    with op.batch_alter_table('posts') as b:
        b.add_column(sa.Column('rating_voice', sa.Integer(), nullable=True))
        b.add_column(sa.Column('rating_would_post', sa.Boolean(), nullable=True))
        b.add_column(sa.Column('rating_template', sa.Boolean(), nullable=True))
        b.add_column(sa.Column('rated_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table('account_secrets',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('user_id', sa.String(length=32), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('provider', sa.String(length=16), nullable=False),
        sa.Column('ciphertext', sa.Text(), nullable=False),
        sa.Column('last4', sa.String(length=4), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('status_detail', sa.String(length=300), nullable=False),
        sa.Column('validated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'kind'))
    op.create_index('ix_account_secrets_user_id', 'account_secrets', ['user_id'], unique=False)
    op.create_table('account_events',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('user_id', sa.String(length=32), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('detail', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_account_events_user_id', 'account_events', ['user_id'], unique=False)


def downgrade() -> None:
    raise NotImplementedError("v2 does not go back to password accounts; restore a v1 backup instead")
