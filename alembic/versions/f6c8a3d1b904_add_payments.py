"""add payments: payments, allocations, seller wallets, payouts, refunds, rider pay

Revision ID: f6c8a3d1b904
Revises: e5b7f2c8a1d3
Create Date: 2026-10-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f6c8a3d1b904"
down_revision: Union[str, None] = "71fbb43a05f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (type name, values). Values are the Python enum member NAMES, like the rest of this project.
PROVIDER = ["PAYSTACK", "SQUAD"]
ENUMS = {
    "payment_provider": PROVIDER,
    "provider_event_provider": PROVIDER,
    "bank_account_provider": PROVIDER,
    "payout_provider": PROVIDER,
    "discrepancy_provider": PROVIDER,
    "payment_status": ["PENDING", "PROCESSING", "SUCCESSFUL", "FAILED", "CANCELLED", "EXPIRED", "REFUNDED", "PARTIALLY_REFUNDED"],
    "allocation_status": ["PENDING", "ELIGIBLE", "RELEASED", "PARTIALLY_REFUNDED", "REFUNDED", "CANCELLED"],
    "wallet_entry_type": ["SELLER_EARNING_PENDING", "SELLER_FUNDS_RELEASED", "PAYOUT_RESERVATION", "PAYOUT_SUCCESS",
                          "PAYOUT_FAILED_REVERSAL", "REFUND", "ADJUSTMENT"],
    "payout_status": ["REQUESTED", "APPROVED", "PROCESSING", "SUCCESSFUL", "FAILED", "REVERSED", "CANCELLED"],
    "rider_payout_status": ["REQUESTED", "APPROVED", "PROCESSING", "SUCCESSFUL", "FAILED", "REVERSED", "CANCELLED"],
    "refund_status": ["REQUESTED", "APPROVED", "PROCESSING", "SUCCESSFUL", "FAILED", "REJECTED"],
    "bank_account_status": ["PENDING", "VERIFIED", "DISABLED"],
    "rider_bank_account_status": ["PENDING", "VERIFIED", "DISABLED"],
    "discrepancy_status": ["OPEN", "RESOLVED"],
}


def E(name: str) -> postgresql.ENUM:
    """A reference to an enum type created below (so tables never try to create it a second time)."""
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table(
        "payments",
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("food_order_id", sa.UUID(), nullable=True),
        sa.Column("provider", E("payment_provider"), nullable=False),
        sa.Column("transaction_ref", sa.String(length=120), nullable=False),
        sa.Column("provider_ref", sa.String(length=160), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", E("payment_status"), nullable=False),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("provider_status", sa.String(length=80), nullable=True),
        sa.Column("provider_payload", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("(order_id IS NOT NULL) <> (food_order_id IS NOT NULL)", name=op.f("ck_payments_one_target")),
        sa.ForeignKeyConstraint(["customer_id"], ["users.id"], name=op.f("fk_payments_customer_id_users")),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name=op.f("fk_payments_order_id_orders")),
        sa.ForeignKeyConstraint(["food_order_id"], ["food_orders.id"], name=op.f("fk_payments_food_order_id_food_orders")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint("transaction_ref", name="uq_payments_transaction_ref"),
    )
    op.create_index(op.f("ix_payments_customer_id"), "payments", ["customer_id"])
    op.create_index(op.f("ix_payments_order_id"), "payments", ["order_id"])
    op.create_index(op.f("ix_payments_food_order_id"), "payments", ["food_order_id"])
    op.create_index(op.f("ix_payments_transaction_ref"), "payments", ["transaction_ref"], unique=True)
    op.create_index(op.f("ix_payments_provider_ref"), "payments", ["provider_ref"])
    op.create_index(op.f("ix_payments_status"), "payments", ["status"])

    op.create_table(
        "payment_allocations",
        sa.Column("payment_id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("seller_order_id", sa.UUID(), nullable=True),
        sa.Column("food_order_id", sa.UUID(), nullable=True),
        sa.Column("seller_id", sa.UUID(), nullable=False),
        sa.Column("gross_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("refunded_gross_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("platform_commission_minor", sa.BigInteger(), nullable=False),
        sa.Column("provider_fee_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tax_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("seller_net_minor", sa.BigInteger(), nullable=False),
        sa.Column("status", E("allocation_status"), nullable=False),
        sa.Column("eligible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], name=op.f("fk_payment_allocations_payment_id_payments"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name=op.f("fk_payment_allocations_order_id_orders")),
        sa.ForeignKeyConstraint(["seller_order_id"], ["seller_orders.id"], name=op.f("fk_payment_allocations_seller_order_id_seller_orders")),
        sa.ForeignKeyConstraint(["food_order_id"], ["food_orders.id"], name=op.f("fk_payment_allocations_food_order_id_food_orders")),
        sa.ForeignKeyConstraint(["seller_id"], ["sellers.id"], name=op.f("fk_payment_allocations_seller_id_sellers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_allocations")),
        sa.UniqueConstraint("payment_id", "seller_order_id", name="uq_payment_allocations_payment_seller_order"),
        sa.UniqueConstraint("payment_id", "food_order_id", name="uq_payment_allocations_payment_food_order"),
    )
    for col in ("payment_id", "order_id", "seller_order_id", "food_order_id", "seller_id", "status", "eligible_at"):
        op.create_index(op.f(f"ix_payment_allocations_{col}"), "payment_allocations", [col])

    op.create_table(
        "seller_wallets",
        sa.Column("seller_id", sa.UUID(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("available_minor", sa.BigInteger(), nullable=False),
        sa.Column("pending_minor", sa.BigInteger(), nullable=False),
        sa.Column("locked_minor", sa.BigInteger(), nullable=False),
        sa.Column("lifetime_credits_minor", sa.BigInteger(), nullable=False),
        sa.Column("lifetime_debits_minor", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["seller_id"], ["sellers.id"], name=op.f("fk_seller_wallets_seller_id_sellers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller_wallets")),
        sa.UniqueConstraint("seller_id", "currency", name="uq_seller_wallets_seller_currency"),
    )
    op.create_index(op.f("ix_seller_wallets_seller_id"), "seller_wallets", ["seller_id"])

    op.create_table(
        "seller_bank_accounts",
        sa.Column("seller_id", sa.UUID(), nullable=False),
        sa.Column("provider", E("bank_account_provider"), nullable=False),
        sa.Column("bank_code", sa.String(length=20), nullable=False),
        sa.Column("bank_name", sa.String(length=120), nullable=True),
        sa.Column("account_number_encrypted", sa.Text(), nullable=False),
        sa.Column("account_number_hash", sa.String(length=128), nullable=False),
        sa.Column("account_number_last4", sa.String(length=4), nullable=False),
        sa.Column("account_name", sa.String(length=200), nullable=False),
        sa.Column("provider_recipient_code", sa.String(length=160), nullable=True),
        sa.Column("status", E("bank_account_status"), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["seller_id"], ["sellers.id"], name=op.f("fk_seller_bank_accounts_seller_id_sellers")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller_bank_accounts")),
        sa.UniqueConstraint("seller_id", "account_number_hash", name="uq_seller_bank_accounts_seller_account"),
    )
    op.create_index(op.f("ix_seller_bank_accounts_seller_id"), "seller_bank_accounts", ["seller_id"])
    op.create_index(op.f("ix_seller_bank_accounts_account_number_hash"), "seller_bank_accounts", ["account_number_hash"])

    op.create_table(
        "payouts",
        sa.Column("seller_id", sa.UUID(), nullable=False),
        sa.Column("wallet_id", sa.UUID(), nullable=False),
        sa.Column("bank_account_id", sa.UUID(), nullable=False),
        sa.Column("reference", sa.String(length=160), nullable=False),
        sa.Column("provider", E("payout_provider"), nullable=False),
        sa.Column("provider_reference", sa.String(length=160), nullable=True),
        sa.Column("requested_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("fee_minor", sa.BigInteger(), nullable=False),
        sa.Column("net_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", E("payout_status"), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("provider_payload", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["seller_id"], ["sellers.id"], name=op.f("fk_payouts_seller_id_sellers")),
        sa.ForeignKeyConstraint(["wallet_id"], ["seller_wallets.id"], name=op.f("fk_payouts_wallet_id_seller_wallets")),
        sa.ForeignKeyConstraint(["bank_account_id"], ["seller_bank_accounts.id"], name=op.f("fk_payouts_bank_account_id_seller_bank_accounts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payouts")),
        sa.UniqueConstraint("reference", name="uq_payouts_reference"),
    )
    for col in ("seller_id", "wallet_id", "bank_account_id", "reference", "provider_reference", "status"):
        op.create_index(op.f(f"ix_payouts_{col}"), "payouts", [col])

    op.create_table(
        "refunds",
        sa.Column("payment_id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("food_order_id", sa.UUID(), nullable=True),
        sa.Column("seller_order_id", sa.UUID(), nullable=True),
        sa.Column("requested_by", sa.UUID(), nullable=False),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("reference", sa.String(length=160), nullable=False),
        sa.Column("provider_refund_reference", sa.String(length=160), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("status", E("refund_status"), nullable=False),
        sa.Column("admin_note", sa.String(length=500), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], name=op.f("fk_refunds_payment_id_payments")),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name=op.f("fk_refunds_order_id_orders")),
        sa.ForeignKeyConstraint(["food_order_id"], ["food_orders.id"], name=op.f("fk_refunds_food_order_id_food_orders")),
        sa.ForeignKeyConstraint(["seller_order_id"], ["seller_orders.id"], name=op.f("fk_refunds_seller_order_id_seller_orders")),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], name=op.f("fk_refunds_requested_by_users")),
        sa.ForeignKeyConstraint(["reviewed_by"], ["admins.id"], name=op.f("fk_refunds_reviewed_by_admins")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refunds")),
        sa.UniqueConstraint("reference", name="uq_refunds_reference"),
    )
    for col in ("payment_id", "order_id", "food_order_id", "seller_order_id", "reference", "status"):
        op.create_index(op.f(f"ix_refunds_{col}"), "refunds", [col])

    op.create_table(
        "wallet_entries",
        sa.Column("wallet_id", sa.UUID(), nullable=False),
        sa.Column("seller_id", sa.UUID(), nullable=False),
        sa.Column("entry_type", E("wallet_entry_type"), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payment_id", sa.UUID(), nullable=True),
        sa.Column("allocation_id", sa.UUID(), nullable=True),
        sa.Column("payout_id", sa.UUID(), nullable=True),
        sa.Column("refund_id", sa.UUID(), nullable=True),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("seller_order_id", sa.UUID(), nullable=True),
        sa.Column("food_order_id", sa.UUID(), nullable=True),
        sa.Column("reference", sa.String(length=160), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["wallet_id"], ["seller_wallets.id"], name=op.f("fk_wallet_entries_wallet_id_seller_wallets")),
        sa.ForeignKeyConstraint(["seller_id"], ["sellers.id"], name=op.f("fk_wallet_entries_seller_id_sellers")),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], name=op.f("fk_wallet_entries_payment_id_payments")),
        sa.ForeignKeyConstraint(["allocation_id"], ["payment_allocations.id"], name=op.f("fk_wallet_entries_allocation_id_payment_allocations")),
        sa.ForeignKeyConstraint(["payout_id"], ["payouts.id"], name=op.f("fk_wallet_entries_payout_id_payouts")),
        sa.ForeignKeyConstraint(["refund_id"], ["refunds.id"], name=op.f("fk_wallet_entries_refund_id_refunds")),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name=op.f("fk_wallet_entries_order_id_orders")),
        sa.ForeignKeyConstraint(["seller_order_id"], ["seller_orders.id"], name=op.f("fk_wallet_entries_seller_order_id_seller_orders")),
        sa.ForeignKeyConstraint(["food_order_id"], ["food_orders.id"], name=op.f("fk_wallet_entries_food_order_id_food_orders")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallet_entries")),
    )
    for col in ("wallet_id", "seller_id", "entry_type", "payment_id", "allocation_id", "payout_id", "refund_id",
                "order_id", "seller_order_id", "food_order_id", "reference"):
        op.create_index(op.f(f"ix_wallet_entries_{col}"), "wallet_entries", [col])
    op.create_index("ix_wallet_entries_wallet_created", "wallet_entries", ["wallet_id", "created_at"])

    op.create_table(
        "payment_provider_events",
        sa.Column("provider", E("provider_event_provider"), nullable=False),
        sa.Column("event_id", sa.String(length=200), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("payment_reference", sa.String(length=160), nullable=True),
        sa.Column("payload_hash", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_provider_events")),
        sa.UniqueConstraint("provider", "event_id", name="uq_payment_provider_events_provider_event"),
    )
    op.create_index(op.f("ix_payment_provider_events_payment_reference"), "payment_provider_events", ["payment_reference"])

    op.create_table(
        "payment_discrepancies",
        sa.Column("payment_id", sa.UUID(), nullable=True),
        sa.Column("provider", E("discrepancy_provider"), nullable=False),
        sa.Column("reference", sa.String(length=160), nullable=False),
        sa.Column("discrepancy_type", sa.String(length=100), nullable=False),
        sa.Column("expected_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("actual_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("status", E("discrepancy_status"), nullable=False),
        sa.Column("resolved_by", sa.UUID(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], name=op.f("fk_payment_discrepancies_payment_id_payments")),
        sa.ForeignKeyConstraint(["resolved_by"], ["admins.id"], name=op.f("fk_payment_discrepancies_resolved_by_admins")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_discrepancies")),
    )
    for col in ("payment_id", "reference", "status"):
        op.create_index(op.f(f"ix_payment_discrepancies_{col}"), "payment_discrepancies", [col])

    op.create_table(
        "rider_bank_accounts",
        sa.Column("rider_id", sa.UUID(), nullable=False),
        sa.Column("bank_code", sa.String(length=20), nullable=False),
        sa.Column("bank_name", sa.String(length=120), nullable=True),
        sa.Column("account_number_encrypted", sa.Text(), nullable=False),
        sa.Column("account_number_hash", sa.String(length=128), nullable=False),
        sa.Column("account_number_last4", sa.String(length=4), nullable=False),
        sa.Column("account_name", sa.String(length=200), nullable=False),
        sa.Column("provider_recipient_code", sa.String(length=160), nullable=True),
        sa.Column("status", E("rider_bank_account_status"), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["rider_id"], ["riders.id"], name=op.f("fk_rider_bank_accounts_rider_id_riders")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rider_bank_accounts")),
        sa.UniqueConstraint("rider_id", "account_number_hash", name="uq_rider_bank_accounts_rider_account"),
    )
    op.create_index(op.f("ix_rider_bank_accounts_rider_id"), "rider_bank_accounts", ["rider_id"])
    op.create_index(op.f("ix_rider_bank_accounts_account_number_hash"), "rider_bank_accounts", ["account_number_hash"])

    op.create_table(
        "rider_payouts",
        sa.Column("rider_id", sa.UUID(), nullable=False),
        sa.Column("bank_account_id", sa.UUID(), nullable=False),
        sa.Column("reference", sa.String(length=160), nullable=False),
        sa.Column("provider_reference", sa.String(length=160), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("deliveries_count", sa.Integer(), nullable=False),
        sa.Column("status", E("rider_payout_status"), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("provider_payload", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.UUID(), nullable=True),
        sa.Column("processing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["rider_id"], ["riders.id"], name=op.f("fk_rider_payouts_rider_id_riders")),
        sa.ForeignKeyConstraint(["bank_account_id"], ["rider_bank_accounts.id"], name=op.f("fk_rider_payouts_bank_account_id_rider_bank_accounts")),
        sa.ForeignKeyConstraint(["approved_by"], ["admins.id"], name=op.f("fk_rider_payouts_approved_by_admins")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rider_payouts")),
        sa.UniqueConstraint("reference", name="uq_rider_payouts_reference"),
    )
    for col in ("rider_id", "bank_account_id", "reference", "provider_reference", "status"):
        op.create_index(op.f(f"ix_rider_payouts_{col}"), "rider_payouts", [col])

    op.create_table(
        "rider_earnings",
        sa.Column("rider_id", sa.UUID(), nullable=False),
        sa.Column("food_order_id", sa.UUID(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("earned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payout_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["rider_id"], ["riders.id"], name=op.f("fk_rider_earnings_rider_id_riders")),
        sa.ForeignKeyConstraint(["food_order_id"], ["food_orders.id"], name=op.f("fk_rider_earnings_food_order_id_food_orders")),
        sa.ForeignKeyConstraint(["payout_id"], ["rider_payouts.id"], name=op.f("fk_rider_earnings_payout_id_rider_payouts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rider_earnings")),
        sa.UniqueConstraint("food_order_id", name="uq_rider_earnings_food_order_id"),
    )
    for col in ("rider_id", "earned_at", "payout_id"):
        op.create_index(op.f(f"ix_rider_earnings_{col}"), "rider_earnings", [col])


def downgrade() -> None:
    for table in ("rider_earnings", "rider_payouts", "rider_bank_accounts", "payment_discrepancies", "payment_provider_events",
                  "wallet_entries", "refunds", "payouts", "seller_bank_accounts", "seller_wallets", "payment_allocations", "payments"):
        op.drop_table(table)
    bind = op.get_bind()
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).drop(bind, checkfirst=True)
