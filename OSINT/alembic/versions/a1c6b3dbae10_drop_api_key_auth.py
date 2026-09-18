"""drop api key auth

Revision ID: a1c6b3dbae10
Revises: f5892256a9ab
Create Date: 2026-09-09 16:40:00.000000

This service has no API-key auth any more (see app/auth.py's removal) --
drops the api_keys/rate_limit_buckets tables and every api_key_id column
that referenced them.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1c6b3dbae10'
down_revision: str | Sequence[str] | None = 'f5892256a9ab'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column('investigations', 'api_key_id')
    op.drop_index(op.f('ix_audit_log_api_key_id'), table_name='audit_log')
    op.drop_column('audit_log', 'api_key_id')
    op.drop_column('audit_log', 'client_name')
    op.drop_table('rate_limit_buckets')
    op.drop_index(op.f('ix_api_keys_key_hash'), table_name='api_keys')
    op.drop_table('api_keys')


def downgrade() -> None:
    op.create_table('api_keys',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('client_name', sa.String(length=100), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('key_prefix', sa.String(length=12), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('rate_limit_per_minute', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_api_keys_key_hash'), 'api_keys', ['key_hash'], unique=True)
    op.create_table('rate_limit_buckets',
    sa.Column('api_key_id', sa.String(length=36), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('request_count', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('api_key_id', 'window_start')
    )
    op.add_column('audit_log', sa.Column('client_name', sa.String(length=100), nullable=True))
    op.add_column('audit_log', sa.Column('api_key_id', sa.String(length=36), nullable=True))
    op.create_index(op.f('ix_audit_log_api_key_id'), 'audit_log', ['api_key_id'], unique=False)
    op.add_column('investigations', sa.Column('api_key_id', sa.String(length=36), nullable=True))
