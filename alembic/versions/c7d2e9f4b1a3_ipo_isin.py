"""isin identifier on ipos

Revision ID: c7d2e9f4b1a3
Revises: a3f9c1d84e02
Create Date: 2026-09-28 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d2e9f4b1a3'
down_revision: Union[str, Sequence[str], None] = 'a3f9c1d84e02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ipos', sa.Column('isin', sa.String(length=20), nullable=False, server_default=''))
    op.create_index('ix_ipos_isin', 'ipos', ['isin'])


def downgrade() -> None:
    op.drop_index('ix_ipos_isin', table_name='ipos')
    op.drop_column('ipos', 'isin')
