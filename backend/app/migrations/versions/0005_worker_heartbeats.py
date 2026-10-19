"""worker heartbeats: one row per running worker

Revision ID: 0005_worker_heartbeats
Revises: 0004_job_attempts
Create Date: 2026-10-19 12:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0005_worker_heartbeats'
down_revision = '0004_job_attempts'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Expand only: a new table. The `worker_report` setting it replaces stays
    # written by render workers for one release, so an API from before this
    # revision (a rollback) still reads what it always read.
    op.create_table('worker_heartbeats',
    sa.Column('id', sa.String(length=128), nullable=False),
    sa.Column('lanes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('started_at', sa.Float(), nullable=False),
    sa.Column('seen_at', sa.Float(), nullable=False),
    sa.Column('separation', sa.Boolean(), nullable=False),
    sa.Column('model', sa.String(length=80), nullable=False),
    sa.Column('commit', sa.String(length=64), nullable=True),
    sa.Column('disk', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_worker_heartbeats_seen', 'worker_heartbeats', ['seen_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_worker_heartbeats_seen', table_name='worker_heartbeats')
    op.drop_table('worker_heartbeats')
