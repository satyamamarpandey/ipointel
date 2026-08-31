"""clerk identity fields and sheets sync outbox

Revision ID: a3f9c1d84e02
Revises: eb560e7c7c5c
Create Date: 2026-08-31 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f9c1d84e02'
down_revision: Union[str, Sequence[str], None] = 'eb560e7c7c5c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('waitlist_leads', sa.Column('clerk_user_id', sa.String(length=80), nullable=False, server_default=''))
    op.add_column('waitlist_leads', sa.Column('identity_provider', sa.String(length=20), nullable=False, server_default=''))
    op.add_column('waitlist_leads', sa.Column('campaign', sa.String(length=80), nullable=False, server_default=''))
    op.add_column('waitlist_leads', sa.Column('page_path', sa.String(length=160), nullable=False, server_default=''))
    op.create_index('ix_waitlist_leads_clerk_user_id', 'waitlist_leads', ['clerk_user_id'])

    op.create_table('sheets_sync_outbox',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('lead_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='PENDING'),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=False, server_default=''),
        sa.Column('sheet_row_number', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['lead_id'], ['waitlist_leads.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('lead_id'),
    )
    op.create_index('ix_sheets_sync_outbox_lead_id', 'sheets_sync_outbox', ['lead_id'])
    op.create_index('ix_sheets_sync_outbox_status', 'sheets_sync_outbox', ['status'])


def downgrade() -> None:
    op.drop_index('ix_sheets_sync_outbox_status', table_name='sheets_sync_outbox')
    op.drop_index('ix_sheets_sync_outbox_lead_id', table_name='sheets_sync_outbox')
    op.drop_table('sheets_sync_outbox')
    op.drop_index('ix_waitlist_leads_clerk_user_id', table_name='waitlist_leads')
    op.drop_column('waitlist_leads', 'page_path')
    op.drop_column('waitlist_leads', 'campaign')
    op.drop_column('waitlist_leads', 'identity_provider')
    op.drop_column('waitlist_leads', 'clerk_user_id')
