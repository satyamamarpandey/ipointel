"""grading state on prediction_outcomes

Revision ID: d8e1f2a3b4c5
Revises: c7d2e9f4b1a3
Create Date: 2026-09-29 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8e1f2a3b4c5'
down_revision: Union[str, Sequence[str], None] = 'c7d2e9f4b1a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('prediction_outcomes', sa.Column('grading_status', sa.String(length=40), nullable=False, server_default=''))
    op.add_column('prediction_outcomes', sa.Column('grading_note', sa.Text(), nullable=False, server_default=''))
    op.add_column('prediction_outcomes', sa.Column('graded_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_prediction_outcomes_grading_status', 'prediction_outcomes', ['grading_status'])


def downgrade() -> None:
    op.drop_index('ix_prediction_outcomes_grading_status', table_name='prediction_outcomes')
    op.drop_column('prediction_outcomes', 'graded_at')
    op.drop_column('prediction_outcomes', 'grading_note')
    op.drop_column('prediction_outcomes', 'grading_status')
