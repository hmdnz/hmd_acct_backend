from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.accounting.models import (
    Account,
    AccountingSettings,
    AccountType,
    TaxRate,
    TaxRateType,
)

DEFAULT_COA = [
    # 1000s: ASSETS
    {"code": "1000", "name": "Current Assets", "type": AccountType.ASSET, "parent_code": None, "desc": "Current Assets Group"},
    {"code": "1010", "name": "Operating Settlement Bank Account", "type": AccountType.ASSET, "parent_code": "1000", "desc": "Main settlement corporate bank account"},
    {"code": "1020", "name": "Payment Gateway Clearing Account", "type": AccountType.ASSET, "parent_code": "1000", "desc": "In-transit customer collections from Paystack/Stripe/OPay"},
    {"code": "1030", "name": "Accounts Receivable", "type": AccountType.ASSET, "parent_code": "1000", "desc": "Corporate receivables and partner settlements"},
    {"code": "1040", "name": "Merchant Reserve Funds Held", "type": AccountType.ASSET, "parent_code": "1000", "desc": "Escrowed dispute safety reserves held against chargebacks"},

    # 2000s: LIABILITIES
    {"code": "2000", "name": "Current Liabilities", "type": AccountType.LIABILITY, "parent_code": None, "desc": "Current Liabilities Group"},
    {"code": "2010", "name": "Customer Escrow Liability (Vendor Clearing)", "type": AccountType.LIABILITY, "parent_code": "2000", "desc": "Customer deposits held in escrow pending order fulfillment"},
    {"code": "2020", "name": "Customer Shipping Fee Liability", "type": AccountType.LIABILITY, "parent_code": "2000", "desc": "Shipping fees collected from customers pending dispatch"},
    {"code": "2030", "name": "Merchant Payables", "type": AccountType.LIABILITY, "parent_code": "2000", "desc": "Net withdrawable funds owed to verified sellers post-delivery"},
    {"code": "2040", "name": "Customer Refunds Payable", "type": AccountType.LIABILITY, "parent_code": "2000", "desc": "Store credits and unrefunded customer claims"},
    {"code": "2050", "name": "VAT Payable", "type": AccountType.LIABILITY, "parent_code": "2000", "desc": "Value Added Tax collected payable to tax authority"},
    {"code": "2055", "name": "Withholding Tax (WHT) Payable", "type": AccountType.LIABILITY, "parent_code": "2000", "desc": "Withholding tax deducted from seller disbursements"},
    {"code": "2060", "name": "Unearned Platform Revenue", "type": AccountType.LIABILITY, "parent_code": "2000", "desc": "Prepaid featured advertising or seller subscriptions"},

    # 3000s: EQUITY
    {"code": "3000", "name": "Equity", "type": AccountType.EQUITY, "parent_code": None, "desc": "Shareholders Equity Group"},
    {"code": "3010", "name": "Contributed Share Capital", "type": AccountType.EQUITY, "parent_code": "3000", "desc": "Paid-in equity capital"},
    {"code": "3020", "name": "Retained Earnings", "type": AccountType.EQUITY, "parent_code": "3000", "desc": "Cumulative prior period net earnings"},

    # 4000s: REVENUE (IFRS 15 Agent Recognition)
    {"code": "4000", "name": "Platform Operating Revenues", "type": AccountType.REVENUE, "parent_code": None, "desc": "Operating Income Group"},
    {"code": "4010", "name": "Platform Commission Revenue", "type": AccountType.REVENUE, "parent_code": "4000", "desc": "Marketplace commission earned upon order delivery (IFRS 15 Agent)"},
    {"code": "4020", "name": "Fulfillment & Shipping Fee Revenue", "type": AccountType.REVENUE, "parent_code": "4000", "desc": "Platform logistics & dispatch service fees earned"},
    {"code": "4030", "name": "Advertising & Merchant Promotion", "type": AccountType.REVENUE, "parent_code": "4000", "desc": "Seller advertising and featured catalog listing fees"},
    {"code": "4040", "name": "Payment Processing Surcharges", "type": AccountType.REVENUE, "parent_code": "4000", "desc": "Transaction convenience fees charged to buyers"},

    # 5000s: EXPENSES
    {"code": "5000", "name": "Direct & Operating Expenses", "type": AccountType.EXPENSE, "parent_code": None, "desc": "Operating Expenses Group"},
    {"code": "5010", "name": "Payment Gateway Processing Fees", "type": AccountType.EXPENSE, "parent_code": "5000", "desc": "Merchant acquiring charges from Paystack/Stripe"},
    {"code": "5020", "name": "Logistics & Courier Fulfillment Outflows", "type": AccountType.EXPENSE, "parent_code": "5000", "desc": "Disbursements to third-party delivery dispatchers"},
    {"code": "5030", "name": "Platform Hosting & IT Infrastructure", "type": AccountType.EXPENSE, "parent_code": "5000", "desc": "Server, database, AWS, Redis, and software operating costs"},
    {"code": "5040", "name": "Dispute, Return & Loss Absorption", "type": AccountType.EXPENSE, "parent_code": "5000", "desc": "Platform-absorbed chargebacks and customer dispute claims"},
]

DEFAULT_TAX_RATES = [
    {"name": "Standard VAT (7.5%)", "rate": Decimal("0.0750"), "jurisdiction": "NG", "type": TaxRateType.VAT},
    {"name": "Vendor Withholding Tax (5.0%)", "rate": Decimal("0.0500"), "jurisdiction": "NG", "type": TaxRateType.WHT},
    {"name": "Zero-Rated VAT (0.0%)", "rate": Decimal("0.0000"), "jurisdiction": "GLOBAL", "type": TaxRateType.VAT},
]


async def seed_chart_of_accounts(db: AsyncSession, currency: str = "NGN") -> dict[str, Account]:
    """
    Idempotent initialization of default Chart of Accounts, Tax Rates, and Accounting Settings.
    """
    code_map: dict[str, Account] = {}

    # 1. Parents first
    for item in DEFAULT_COA:
        if item["parent_code"] is None:
            existing = (await db.execute(select(Account).where(Account.code == item["code"]))).scalar_one_or_none()
            if not existing:
                account = Account(
                    code=item["code"],
                    name=item["name"],
                    type=item["type"],
                    description=item["desc"],
                    is_active=True,
                    currency=currency,
                )
                db.add(account)
                await db.flush()
                code_map[item["code"]] = account
            else:
                code_map[item["code"]] = existing

    # 2. Child accounts
    for item in DEFAULT_COA:
        if item["parent_code"] is not None:
            existing = (await db.execute(select(Account).where(Account.code == item["code"]))).scalar_one_or_none()
            if not existing:
                parent_acc = code_map.get(item["parent_code"])
                account = Account(
                    code=item["code"],
                    name=item["name"],
                    type=item["type"],
                    parent_id=parent_acc.id if parent_acc else None,
                    description=item["desc"],
                    is_active=True,
                    currency=currency,
                )
                db.add(account)
                await db.flush()
                code_map[item["code"]] = account
            else:
                code_map[item["code"]] = existing

    # 3. Tax Rates
    for t_item in DEFAULT_TAX_RATES:
        t_exist = (await db.execute(select(TaxRate).where(TaxRate.name == t_item["name"]))).scalar_one_or_none()
        if not t_exist:
            tax_rate = TaxRate(
                name=t_item["name"],
                rate=t_item["rate"],
                jurisdiction=t_item["jurisdiction"],
                type=t_item["type"],
                is_active=True,
            )
            db.add(tax_rate)

    # 4. Accounting Settings Singleton
    settings_exist = (await db.execute(select(AccountingSettings))).scalar_one_or_none()
    if not settings_exist:
        initial_settings = AccountingSettings(
            legal_entity_name="Hamads Marketplace Ltd",
            functional_currency=currency,
            fiscal_year_end_month=12,
            standard_commission_rate=Decimal("0.1000"),
            standard_reserve_rate=Decimal("0.0500"),
            reserve_hold_days=14,
        )
        db.add(initial_settings)

    await db.commit()
    return code_map
