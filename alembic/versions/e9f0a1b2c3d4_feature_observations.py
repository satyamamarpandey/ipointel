"""feature_observations table and market-data attempt state on ipos

Revision ID: e9f0a1b2c3d4
Revises: d8e1f2a3b4c5
Create Date: 2026-09-29 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e9f0a1b2c3d4'
down_revision: Union[str, Sequence[str], None] = 'd8e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ipos', sa.Column('market_data_status', sa.String(length=30), nullable=False, server_default=''))
    op.add_column('ipos', sa.Column('market_data_checked_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_ipos_market_data_status', 'ipos', ['market_data_status'])
    op.create_table(
        'feature_observations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ipo_id', sa.Integer(), sa.ForeignKey('ipos.id'), nullable=False),
        sa.Column('field_name', sa.String(length=60), nullable=False),
        sa.Column('value', sa.Float(), nullable=True),
        sa.Column('unit', sa.String(length=20), nullable=False, server_default=''),
        sa.Column('source_name', sa.String(length=100), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=False, server_default=''),
        sa.Column('source_tier', sa.Integer(), nullable=False, server_default='2'),
        sa.Column('source_form', sa.String(length=20), nullable=False, server_default=''),
        sa.Column('period_start', sa.String(length=10), nullable=False, server_default=''),
        sa.Column('period_end', sa.String(length=10), nullable=False, server_default=''),
        sa.Column('available_at', sa.String(length=10), nullable=False, server_default=''),
        sa.Column('availability_rule', sa.String(length=40), nullable=False, server_default=''),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False, server_default='1.0'),
        sa.Column('event_stage', sa.String(length=40), nullable=False, server_default=''),
        sa.Column('raw', sa.JSON(), nullable=True),
        sa.UniqueConstraint('ipo_id', 'field_name', 'period_end', 'source_name', name='uq_feature_obs'),
    )
    op.create_index('ix_feature_observations_ipo_id', 'feature_observations', ['ipo_id'])
    op.create_index('ix_feature_observations_field_name', 'feature_observations', ['field_name'])
    op.create_index('ix_feature_observations_period_end', 'feature_observations', ['period_end'])
    op.create_index('ix_feature_observations_available_at', 'feature_observations', ['available_at'])


def downgrade() -> None:
    op.drop_table('feature_observations')
    op.drop_index('ix_ipos_market_data_status', table_name='ipos')
    op.drop_column('ipos', 'market_data_checked_at')
    op.drop_column('ipos', 'market_data_status')
