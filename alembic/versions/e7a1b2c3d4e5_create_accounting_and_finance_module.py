"""create accounting and finance module tables

Revision ID: e7a1b2c3d4e5
Revises: 71fbb43a05f9
Create Date: 2026-11-02 10:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e7a1b2c3d4e5'
down_revision: Union[str, None] = 'f6c8a3d1b904'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. ENUMS
    account_type = postgresql.ENUM('ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE', name='account_type', create_type=False)
    journal_entry_status = postgresql.ENUM('DRAFT', 'POSTED', 'VOIDED', name='journal_entry_status', create_type=False)
    transaction_type = postgresql.ENUM(
        'ORDER_PAYMENT', 'ESCROW_RELEASE', 'VENDOR_PAYOUT', 'REFUND', 'DISPUTE_HOLD',
        'RESERVE_RELEASE', 'GATEWAY_SETTLEMENT', 'EXPENSE_PAYMENT', 'ADJUSTMENT', 'OPENING_BALANCE',
        name='transaction_type', create_type=False
    )
    sub_ledger_type = postgresql.ENUM('VENDOR', 'CUSTOMER', 'GATEWAY', 'EXPENSE_VENDOR', 'NONE', name='sub_ledger_type', create_type=False)
    fiscal_period_status = postgresql.ENUM('OPEN', 'LOCKED', 'CLOSED', name='fiscal_period_status', create_type=False)
    payment_tx_status = postgresql.ENUM('INITIATED', 'AUTHORIZED', 'CAPTURED', 'FAILED', 'REFUNDED', 'PARTIALLY_REFUNDED', 'DISPUTED', name='payment_transaction_status', create_type=False)
    refund_status = postgresql.ENUM('PENDING', 'APPROVED', 'COMPLETED', 'FAILED', 'REJECTED', name='refund_status', create_type=False)
    payout_status = postgresql.ENUM('PENDING', 'APPROVED', 'PROCESSING', 'COMPLETED', 'FAILED', 'CANCELLED', name='vendor_payout_status', create_type=False)
    expense_status = postgresql.ENUM('DRAFT', 'APPROVED', 'PAID', 'CANCELLED', name='expense_bill_status', create_type=False)
    tax_rate_type = postgresql.ENUM('VAT', 'WHT', 'SALES_TAX', name='tax_rate_type', create_type=False)
    reconciliation_status = postgresql.ENUM('UNMATCHED', 'MATCHED', 'EXCEPTION', name='reconciliation_status', create_type=False)

    for en in [
        account_type, journal_entry_status, transaction_type, sub_ledger_type,
        fiscal_period_status, payment_tx_status, refund_status, payout_status,
        expense_status, tax_rate_type, reconciliation_status
    ]:
        en.create(op.get_bind(), checkfirst=True)

    # 2. accounting_settings
    op.create_table(
        'accounting_settings',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('legal_entity_name', sa.String(length=200), nullable=False),
        sa.Column('functional_currency', sa.String(length=3), nullable=False),
        sa.Column('tax_identification_number', sa.String(length=50), nullable=True),
        sa.Column('fiscal_year_end_month', sa.Integer(), nullable=False),
        sa.Column('standard_commission_rate', sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column('standard_reserve_rate', sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column('reserve_hold_days', sa.Integer(), nullable=False),
        sa.Column('require_dual_approval_payouts', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    # 3. accounting_fiscal_periods
    op.create_table(
        'accounting_fiscal_periods',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(length=50), nullable=False),
        sa.Column('start_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('end_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', fiscal_period_status, nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('closed_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('admins.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('ix_accounting_fiscal_periods_start_date', 'accounting_fiscal_periods', ['start_date'])
    op.create_index('ix_accounting_fiscal_periods_end_date', 'accounting_fiscal_periods', ['end_date'])

    # 4. accounts (CoA)
    op.create_table(
        'accounts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('code', sa.String(length=20), unique=True, nullable=False),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('type', account_type, nullable=False),
        sa.Column('parent_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('accounts.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('ix_accounts_code', 'accounts', ['code'])
    op.create_index('ix_accounts_type', 'accounts', ['type'])

    # 5. journal_entries
    op.create_table(
        'journal_entries',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('entry_number', sa.String(length=50), unique=True, nullable=False),
        sa.Column('idempotency_key', sa.String(length=150), unique=True, nullable=True),
        sa.Column('transaction_type', transaction_type, nullable=False),
        sa.Column('reference_id', sa.String(length=100), nullable=True),
        sa.Column('narration', sa.String(length=500), nullable=False),
        sa.Column('status', journal_entry_status, nullable=False, server_default='POSTED'),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('posted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('admins.id', ondelete='SET NULL'), nullable=True),
        sa.Column('approved_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('admins.id', ondelete='SET NULL'), nullable=True),
        sa.Column('reverses_entry_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('journal_entries.id', ondelete='SET NULL'), nullable=True),
        sa.Column('reversed_by_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('journal_entries.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('ix_journal_entries_entry_number', 'journal_entries', ['entry_number'])
    op.create_index('ix_journal_entries_posted_at', 'journal_entries', ['posted_at'])
    op.create_index('ix_journal_entries_reference_id', 'journal_entries', ['reference_id'])

    # 6. journal_lines
    op.create_table(
        'journal_lines',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('journal_entry_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('journal_entries.id', ondelete='CASCADE'), nullable=False),
        sa.Column('account_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('accounts.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('sub_ledger_type', sub_ledger_type, nullable=False, server_default='NONE'),
        sa.Column('sub_ledger_id', sa.String(length=100), nullable=True),
        sa.Column('debit', sa.Numeric(precision=18, scale=4), nullable=False, server_default='0.0000'),
        sa.Column('credit', sa.Numeric(precision=18, scale=4), nullable=False, server_default='0.0000'),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('fx_rate', sa.Numeric(precision=12, scale=6), nullable=False, server_default='1.000000'),
        sa.Column('base_debit', sa.Numeric(precision=18, scale=4), nullable=False, server_default='0.0000'),
        sa.Column('base_credit', sa.Numeric(precision=18, scale=4), nullable=False, server_default='0.0000'),
        sa.Column('description', sa.String(length=255), nullable=True),
    )
    op.create_index('ix_journal_lines_journal_entry_id', 'journal_lines', ['journal_entry_id'])
    op.create_index('ix_journal_lines_account_id', 'journal_lines', ['account_id'])
    op.create_index('ix_journal_lines_sub_ledger_id', 'journal_lines', ['sub_ledger_id'])

    # 7. payment_transactions
    op.create_table(
        'payment_transactions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('provider_reference', sa.String(length=150), unique=True, nullable=False),
        sa.Column('idempotency_key', sa.String(length=150), unique=True, nullable=False),
        sa.Column('order_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('orders.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('gross_amount', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('provider_fee', sa.Numeric(precision=18, scale=4), nullable=False, server_default='0.0000'),
        sa.Column('net_amount', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('status', payment_tx_status, nullable=False),
        sa.Column('journal_entry_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('journal_entries.id', ondelete='SET NULL'), nullable=True),
        sa.Column('raw_response', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    # 8. accounting_refunds
    op.create_table(
        'accounting_refunds',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('payment_transaction_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('payment_transactions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('order_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('orders.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('seller_order_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('seller_orders.id', ondelete='SET NULL'), nullable=True),
        sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('amount', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('reason', sa.String(length=255), nullable=False),
        sa.Column('status', refund_status, nullable=False),
        sa.Column('provider_refund_reference', sa.String(length=150), nullable=True),
        sa.Column('journal_entry_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('journal_entries.id', ondelete='SET NULL'), nullable=True),
        sa.Column('approved_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('admins.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    # 9. vendor_payouts
    op.create_table(
        'vendor_payouts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('payout_reference', sa.String(length=100), unique=True, nullable=False),
        sa.Column('vendor_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('sellers.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('amount', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('fee_deducted', sa.Numeric(precision=18, scale=4), nullable=False, server_default='0.0000'),
        sa.Column('net_amount', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('bank_account_details', sa.Text(), nullable=False),
        sa.Column('status', payout_status, nullable=False),
        sa.Column('gateway_reference', sa.String(length=255), nullable=True),
        sa.Column('journal_entry_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('journal_entries.id', ondelete='SET NULL'), nullable=True),
        sa.Column('failure_reason', sa.String(length=500), nullable=True),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('admins.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    # 10. vendor_reserve_holds
    op.create_table(
        'vendor_reserve_holds',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('vendor_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('sellers.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('reference_id', sa.String(length=100), nullable=True),
        sa.Column('hold_amount', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('released_amount', sa.Numeric(precision=18, scale=4), nullable=False, server_default='0.0000'),
        sa.Column('status', sa.String(length=30), nullable=False, server_default='HELD'),
        sa.Column('release_due_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('journal_entry_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('journal_entries.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    # 11. accounting_expenses
    op.create_table(
        'accounting_expenses',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('vendor_name', sa.String(length=150), nullable=False),
        sa.Column('expense_account_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('accounts.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('invoice_number', sa.String(length=100), nullable=True),
        sa.Column('subtotal', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('tax_amount', sa.Numeric(precision=18, scale=4), nullable=False, server_default='0.0000'),
        sa.Column('total_amount', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('status', expense_status, nullable=False),
        sa.Column('due_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('journal_entry_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('journal_entries.id', ondelete='SET NULL'), nullable=True),
        sa.Column('approved_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('admins.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    # 12. tax_rates
    op.create_table(
        'tax_rates',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('rate', sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column('jurisdiction', sa.String(length=100), nullable=False, server_default='NG'),
        sa.Column('type', tax_rate_type, nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    # 13. bank_statement_rows
    op.create_table(
        'bank_statement_rows',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('account_code', sa.String(length=20), nullable=False, server_default='1010'),
        sa.Column('statement_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reference', sa.String(length=150), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('debit', sa.Numeric(precision=18, scale=4), nullable=False, server_default='0.0000'),
        sa.Column('credit', sa.Numeric(precision=18, scale=4), nullable=False, server_default='0.0000'),
        sa.Column('balance', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='NGN'),
        sa.Column('status', reconciliation_status, nullable=False, server_default='UNMATCHED'),
        sa.Column('matched_journal_line_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('journal_lines.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('bank_statement_rows')
    op.drop_table('tax_rates')
    op.drop_table('accounting_expenses')
    op.drop_table('vendor_reserve_holds')
    op.drop_table('vendor_payouts')
    op.drop_table('accounting_refunds')
    op.drop_table('payment_transactions')
    op.drop_table('journal_lines')
    op.drop_table('journal_entries')
    op.drop_table('accounts')
    op.drop_table('accounting_fiscal_periods')
    op.drop_table('accounting_settings')
