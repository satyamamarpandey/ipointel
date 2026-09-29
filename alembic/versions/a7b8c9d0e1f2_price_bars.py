"""price_bars and bhavcopy_days (official NSE daily prices)

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-09-29 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'price_bars',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('isin', sa.String(length=20), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('series', sa.String(length=4), nullable=False, server_default=''),
        sa.Column('trade_date', sa.String(length=10), nullable=False),
        sa.Column('open', sa.Float(), nullable=True),
        sa.Column('close', sa.Float(), nullable=True),
        sa.Column('source_name', sa.String(length=60), nullable=False, server_default='NSE bhavcopy'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('isin', 'trade_date', name='uq_price_bar_isin_date'),
    )
    op.create_index('ix_price_bars_isin', 'price_bars', ['isin'])
    op.create_index('ix_price_bars_symbol', 'price_bars', ['symbol'])
    op.create_index('ix_price_bars_trade_date', 'price_bars', ['trade_date'])
    op.create_table(
        'bhavcopy_days',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('trade_date', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='ok'),
        sa.Column('source_url', sa.Text(), nullable=False, server_default=''),
        sa.Column('rows_stored', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=False, server_default=''),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_bhavcopy_days_trade_date', 'bhavcopy_days', ['trade_date'], unique=True)


def downgrade() -> None:
    op.drop_table('bhavcopy_days')
    op.drop_table('price_bars')
