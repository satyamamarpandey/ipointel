"""extended return windows on performance_snapshots

Revision ID: f1a2b3c4d5e6
Revises: e9f0a1b2c3d4
Create Date: 2026-09-29 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e9f0a1b2c3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FLOATS = ('listing_open_return_pct', 'return_7d_pct', 'return_90d_pct', 'return_24m_pct', 'benchmark_relative_12m_pct')


def upgrade() -> None:
    for col in _FLOATS:
        op.add_column('performance_snapshots', sa.Column(col, sa.Float(), nullable=True))
    op.add_column('performance_snapshots', sa.Column('listing_date_used', sa.String(length=20), nullable=False, server_default=''))


def downgrade() -> None:
    op.drop_column('performance_snapshots', 'listing_date_used')
    for col in reversed(_FLOATS):
        op.drop_column('performance_snapshots', col)
