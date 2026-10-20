"""identity bridge: contact_links table and rooms.contact_key_raw

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('rooms', sa.Column('contact_key_raw', sa.String(length=80), nullable=True))
    op.create_table('contact_links',
    sa.Column('source_id', sa.String(length=32), nullable=False),
    sa.Column('contact_key', sa.String(length=80), nullable=False),
    sa.Column('account_id', sa.String(length=32), nullable=False),
    sa.Column('canonical_key', sa.String(length=80), nullable=False),
    sa.Column('created_at', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('source_id', 'contact_key')
    )
    op.create_index('ix_links_acct_canonical', 'contact_links', ['account_id', 'canonical_key'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_links_acct_canonical', table_name='contact_links')
    op.drop_table('contact_links')
    with op.batch_alter_table('rooms') as batch:
        batch.drop_column('contact_key_raw')
