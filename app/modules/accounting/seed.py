"""Idempotent Chart of Accounts seeder for Hamads Accounting (Nigerian IFRS)."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.accounting.models import Account, AccountType, NormalBalance

# (code, name, account_type, normal_balance, parent_code, is_system, description)
DEFAULT_CHART_OF_ACCOUNTS: list[
    tuple[str, str, AccountType, NormalBalance, str | None, bool, str]
] = [
    # =========================================================================
    # 1000 - ASSETS (Normal Balance: DEBIT)
    # =========================================================================
    ("1000", "Current Assets", AccountType.ASSET, NormalBalance.DEBIT, None, True, "Current Assets header account"),
    ("1010", "Operating Settlement Bank Account", AccountType.ASSET, NormalBalance.DEBIT, "1000", True, "Primary Naira corporate bank account"),
    ("1020", "Payment Gateway Clearing Account", AccountType.ASSET, NormalBalance.DEBIT, "1000", True, "Paystack & Squad unsettled/clearing funds"),
    ("1030", "Petty Cash / Cash Imprest", AccountType.ASSET, NormalBalance.DEBIT, "1000", False, "Office float for minor daily cash expenses"),
    ("1040", "Merchant Reserve Funds Held", AccountType.ASSET, NormalBalance.DEBIT, "1000", True, "Restricted escrow / reserve balances"),
    ("1100", "Accounts Receivable (Trade Debtors)", AccountType.ASSET, NormalBalance.DEBIT, "1000", False, "B2B / corporate client invoices unpaid"),
    ("1110", "Staff Advances & Loans Receivable", AccountType.ASSET, NormalBalance.DEBIT, "1000", False, "Salary advances and staff floats"),
    ("1200", "Prepaid Rent & Insurance", AccountType.ASSET, NormalBalance.DEBIT, "1000", False, "Upfront annual office rent & insurance policies"),
    ("1210", "Withholding Tax (WHT) Receivable", AccountType.ASSET, NormalBalance.DEBIT, "1000", False, "WHT credit notes deducted by corporate clients"),
    ("1220", "Input VAT Receivable", AccountType.ASSET, NormalBalance.DEBIT, "1000", False, "VAT paid on qualifying vendor bills"),
    ("1500", "Non-Current Assets (Property & Equipment)", AccountType.ASSET, NormalBalance.DEBIT, None, False, "Non-Current Assets header account"),
    ("1510", "Computer Equipment & Servers", AccountType.ASSET, NormalBalance.DEBIT, "1500", False, "Laptops, routers, Starlink hardware, POS devices"),
    ("1520", "Office Furniture & Fittings", AccountType.ASSET, NormalBalance.DEBIT, "1500", False, "Desks, chairs, shelving, air conditioners"),
    ("1530", "Motor Vehicles & Delivery Bikes", AccountType.ASSET, NormalBalance.DEBIT, "1500", False, "Company vehicles and logistics fleet"),
    ("1540", "Capitalized Software & Platform Dev", AccountType.ASSET, NormalBalance.DEBIT, "1500", False, "Intangible platform assets (IAS 38)"),
    ("1590", "Accumulated Depreciation & Amortization", AccountType.ASSET, NormalBalance.CREDIT, "1500", False, "Contra-asset for cumulative depreciation"),

    # =========================================================================
    # 2000 - LIABILITIES (Normal Balance: CREDIT)
    # =========================================================================
    ("2000", "Current Liabilities", AccountType.LIABILITY, NormalBalance.CREDIT, None, True, "Current Liabilities header account"),
    ("2010", "Customer Escrow Liability (Vendor Clearing)", AccountType.LIABILITY, NormalBalance.CREDIT, "2000", True, "Funds held during order hold window"),
    ("2020", "Customer Shipping Fee Liability", AccountType.LIABILITY, NormalBalance.CREDIT, "2000", True, "Delivery fees collected pending dispatch"),
    ("2030", "Merchant Payables (Withdrawable Balances)", AccountType.LIABILITY, NormalBalance.CREDIT, "2000", True, "Net vendor earnings payable"),
    ("2040", "Rider Payables (Delivery Clearing)", AccountType.LIABILITY, NormalBalance.CREDIT, "2000", True, "Rider delivery shares payable"),
    ("2100", "Accounts Payable (Trade Creditors)", AccountType.LIABILITY, NormalBalance.CREDIT, "2000", False, "Unpaid supplier, cloud, and utility bills"),
    ("2110", "Accrued Expenses", AccountType.LIABILITY, NormalBalance.CREDIT, "2000", False, "Month-end accruals (unbilled utilities, audit fees)"),
    ("2200", "Output VAT Payable (FIRS)", AccountType.LIABILITY, NormalBalance.CREDIT, "2000", False, "7.5% VAT collected on commissions and services"),
    ("2210", "Withholding Tax (WHT) Payable", AccountType.LIABILITY, NormalBalance.CREDIT, "2000", False, "WHT deducted from contractors, rent, and consultants"),
    ("2220", "PAYE & Staff Statutory Deductions Payable", AccountType.LIABILITY, NormalBalance.CREDIT, "2000", False, "State IRS PAYE, Pension (PenCom), NHF, NSITF"),
    ("2230", "Corporate Income Tax (CIT) Payable", AccountType.LIABILITY, NormalBalance.CREDIT, "2000", False, "FIRS CIT & Education Tax provision"),

    # =========================================================================
    # 3000 - EQUITY (Normal Balance: CREDIT)
    # =========================================================================
    ("3000", "Shareholders' Equity", AccountType.EQUITY, NormalBalance.CREDIT, None, True, "Equity header account"),
    ("3010", "Issued Share Capital", AccountType.EQUITY, NormalBalance.CREDIT, "3000", True, "Ordinary share capital (CAC registered)"),
    ("3020", "Director / Founder Capital Contributions", AccountType.EQUITY, NormalBalance.CREDIT, "3000", False, "Shareholder equity injections"),
    ("3030", "Retained Earnings", AccountType.EQUITY, NormalBalance.CREDIT, "3000", True, "Cumulative prior-year net profits/losses"),
    ("3040", "Current Year Earnings", AccountType.EQUITY, NormalBalance.CREDIT, "3000", True, "Year-to-date net profit/loss"),

    # =========================================================================
    # 4000 - REVENUE (Normal Balance: CREDIT)
    # =========================================================================
    ("4000", "Platform & Service Revenues", AccountType.REVENUE, NormalBalance.CREDIT, None, True, "Revenue header account"),
    ("4010", "Platform Commission Revenue (IFRS 15 Agent)", AccountType.REVENUE, NormalBalance.CREDIT, "4000", True, "Marketplace & food order commission fees"),
    ("4020", "Fulfillment & Shipping Fee Revenue", AccountType.REVENUE, NormalBalance.CREDIT, "4000", True, "Platform margin on delivery/shipping fees"),
    ("4030", "Service & Consulting Fee Revenue", AccountType.REVENUE, NormalBalance.CREDIT, "4000", False, "B2B service fees, subscriptions, listing fees"),
    ("4080", "Other Operating Income", AccountType.REVENUE, NormalBalance.CREDIT, "4000", False, "FX gains, interest income, miscellaneous receipts"),
    ("4090", "Commission Refunds & Reversals", AccountType.REVENUE, NormalBalance.DEBIT, "4000", True, "Contra-revenue on cancelled/refunded orders"),

    # =========================================================================
    # 5000 - COST OF SALES / DIRECT COSTS (Normal Balance: DEBIT)
    # =========================================================================
    ("5000", "Direct Platform & Service Costs", AccountType.EXPENSE, NormalBalance.DEBIT, None, True, "Direct cost of sales header account"),
    ("5010", "Payment Gateway Processing Fees", AccountType.EXPENSE, NormalBalance.DEBIT, "5000", True, "Paystack & Squad transaction/transfer charges"),
    ("5020", "Logistics & Rider Subsidy Expense", AccountType.EXPENSE, NormalBalance.DEBIT, "5000", False, "Delivery fee shortfalls and rider incentives"),
    ("5030", "Cloud Hosting & Server Infrastructure", AccountType.EXPENSE, NormalBalance.DEBIT, "5000", False, "AWS EC2/RDS/S3, Render, Vercel, Neon DB"),
    ("5040", "SMS, OTP & Communication APIs", AccountType.EXPENSE, NormalBalance.DEBIT, "5000", False, "Kudisms, Termii, transactional email costs"),
    ("5050", "Direct Subcontractor & Service Delivery Costs", AccountType.EXPENSE, NormalBalance.DEBIT, "5000", False, "Direct project contractors"),

    # =========================================================================
    # 6000 - OPERATING & ADMINISTRATIVE EXPENSES (Normal Balance: DEBIT)
    # =========================================================================
    ("6000", "General & Administrative Expenses", AccountType.EXPENSE, NormalBalance.DEBIT, None, False, "Operating expenses header account"),
    ("6010", "Salaries, Wages & Allowances", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "Gross monthly staff payroll"),
    ("6020", "Employer Pension & Statutory Contributions", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "Employer 10% PenCom, NSITF, ITF contributions"),
    ("6030", "Staff Welfare, Medical & Training", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "HMO, team meals, staff training"),
    ("6100", "Office Rent & Service Charge", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "Monthly amortized office rent & estate dues"),
    ("6110", "Electricity, Generator Fuel & Diesel", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "DisCo bills, petrol and diesel for office generator"),
    ("6120", "Internet & Telecommunications", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "Starlink subscription, 4G backup data, office lines"),
    ("6130", "Office Cleaning, Sanitation & Security", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "Janitorial supplies, waste disposal, security guards"),
    ("6140", "Repairs & Maintenance", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "Generator servicing, AC maintenance, office repairs"),
    ("6200", "Printing, Stationery & Office Supplies", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "Paper, ink, pens, receipt rolls, packaging supplies"),
    ("6210", "Software Subscriptions & SaaS Tools", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "Productivity tools, domains, SSL, monitoring apps"),
    ("6300", "Bank Charges & Account Maintenance", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "Stamp duty, account maintenance, NIP transfer fees"),
    ("6310", "Legal, Accounting & Professional Fees", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "CAC filings, audit, tax advisory, legal retainers"),
    ("6320", "Regulatory Permits, Licenses & Levies", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "Local government levies, business premises permits"),
    ("6400", "Marketing, Advertising & Promotions", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "Meta/Google ads, influencers, customer promo codes"),
    ("6410", "Local Transport, Fuel & Travel", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "Dispatch runs, staff transport, interstate travel"),
    ("6500", "Depreciation & Amortization Expense", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "Monthly depreciation of fixed & intangible assets"),
    ("6900", "Miscellaneous & General Expenses", AccountType.EXPENSE, NormalBalance.DEBIT, "6000", False, "Uncategorized minor expenses"),
]


async def seed_chart_of_accounts(db: AsyncSession) -> dict[str, int]:
    """
    Idempotently seeds the standard Nigerian IFRS Chart of Accounts.
    Existing accounts matched by `code` are left untouched so UUIDs and
    existing journal lines remain intact.
    """
    existing_result = await db.execute(select(Account))
    code_to_account: dict[str, Account] = {
        acct.code: acct for acct in existing_result.scalars().all()
    }

    created = 0
    skipped = 0

    for (
        code,
        name,
        account_type,
        normal_balance,
        parent_code,
        is_system,
        description,
    ) in DEFAULT_CHART_OF_ACCOUNTS:
        if code in code_to_account:
            skipped += 1
            continue

        parent = code_to_account.get(parent_code) if parent_code else None
        account = Account(
            code=code,
            name=name,
            type=account_type,
            parent_id=parent.id if parent else None,
            currency="NGN",
            is_active=True,
            description=description,
        )
        db.add(account)
        await db.flush()
        code_to_account[code] = account
        created += 1

    await db.commit()
    return {"created": created, "skipped": skipped, "total": len(code_to_account)}