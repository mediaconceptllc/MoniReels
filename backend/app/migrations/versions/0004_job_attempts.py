"""job attempts (started_at, available_at, history) and paid STT chunks

Revision ID: 0004_job_attempts
Revises: 0003_usage_events
Create Date: 2026-10-19 08:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0004_job_attempts'
down_revision = '0003_usage_events'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A retry's backoff, when its current attempt started, and every attempt
    # that ended (app.jobs.queue.requeue). All nullable: a row from before this
    # migration simply has no history, and NULL available_at is "runnable now".
    op.add_column('jobs', sa.Column('started_at', sa.Float(), nullable=True))
    op.add_column('jobs', sa.Column('available_at', sa.Float(), nullable=True))
    op.add_column('jobs', sa.Column('history', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    # What an unfinished transcription already paid for (app.stt.chunk_cache).
    # Cascades with the project; the worker deletes a project's rows once its
    # transcription completes.
    op.create_table('stt_chunks',
    sa.Column('project_id', sa.String(length=32), nullable=False),
    sa.Column('variant', sa.String(length=200), nullable=False),
    sa.Column('digest', sa.String(length=64), nullable=False),
    sa.Column('text', sa.Text(), server_default='', nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('project_id', 'variant', 'digest')
    )


def downgrade() -> None:
    op.drop_table('stt_chunks')
    op.drop_column('jobs', 'history')
    op.drop_column('jobs', 'available_at')
    op.drop_column('jobs', 'started_at')
