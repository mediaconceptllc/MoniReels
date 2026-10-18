"""usage events

Revision ID: 0003_usage_events
Revises: 0002_subtitle_templates
Create Date: 2026-10-18 09:21:19.618651
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0003_usage_events'
down_revision = '0002_subtitle_templates'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The spend ledger (app.metering). Append-only and without foreign keys
    # on purpose: jobs are purged and projects deleted, and neither may take
    # the record of money already spent with them. cost_usd NULL = unknown.
    op.create_table('usage_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('job_id', sa.String(length=32), nullable=True),
    sa.Column('job_kind', sa.String(length=32), server_default='', nullable=False),
    sa.Column('project_id', sa.String(length=32), nullable=True),
    sa.Column('user_id', sa.String(length=32), nullable=True),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('model', sa.String(length=120), server_default='', nullable=False),
    sa.Column('requests', sa.Integer(), server_default='0', nullable=False),
    sa.Column('prompt_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('completion_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('audio_seconds', sa.Float(), server_default='0', nullable=False),
    sa.Column('characters', sa.Integer(), server_default='0', nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_usage_created', 'usage_events', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_usage_created', table_name='usage_events')
    op.drop_table('usage_events')
