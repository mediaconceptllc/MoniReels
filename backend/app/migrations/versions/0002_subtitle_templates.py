"""subtitle templates

Revision ID: 0002_subtitle_templates
Revises: 0001_baseline
Create Date: 2026-08-31 03:01:08.791156
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0002_subtitle_templates'
down_revision = '0001_baseline'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A named subtitle style the whole studio applies. The style is one JSONB
    # document rather than a column per field: a template is written rarely
    # and read whole, never queried by font size, and columns would need a
    # migration every time SubtitleStyle gains a field — which it just did.
    op.create_table('subtitle_templates',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('style', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )


def downgrade() -> None:
    op.drop_table('subtitle_templates')
