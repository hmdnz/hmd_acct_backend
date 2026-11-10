"""Invoice and Payment Receipt Generation Service for Hamads Marketplace.

Produces structured JSON and A4-printable HTML tax invoices and receipts
with legal entity details, Paystack/Squad references, and Nigerian Naira (₦) formatting.
"""

from decimal import Decimal
import uuid
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models import Payment, Payout
from app.modules.orders.models import Order, OrderItem
from app.modules.auth.models import User, Seller


def format_naira(amount_kobo_or_naira: int | float | Decimal, is_kobo: bool = True) -> str:
    """Formats amount as ₦XX,XXX.XX"""
    val = Decimal(str(amount_kobo_or_naira))
    if is_kobo:
        val = val / Decimal("100")
    return f"₦{val:,.2f}"


class ReceiptService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_receipt_data(self, payment_id: uuid.UUID) -> dict[str, Any]:
        """Collects all receipt details for a payment."""
        stmt = (
            select(Payment)
            .where(Payment.id == payment_id)
        )
        res = await self.db.execute(stmt)
        payment = res.scalar_one_or_none()
        if not payment:
            raise ValueError(f"Payment {payment_id} not found")

        # Fetch customer
        cust_stmt = select(User).where(User.id == payment.customer_id)
        customer = (await self.db.execute(cust_stmt)).scalar_one_or_none()
        customer_name = customer.full_name if customer and hasattr(customer, "full_name") else "Valued Customer"
        customer_email = customer.email if customer else "N/A"
        customer_phone = customer.phone_number if customer and hasattr(customer, "phone_number") else "N/A"

        items = []
        subtotal_kobo = 0
        shipping_kobo = 0

        # E-commerce marketplace order
        if payment.order_id:
            order_stmt = (
                select(Order)
                .options(selectinload(Order.items))
                .where(Order.id == payment.order_id)
            )
            order = (await self.db.execute(order_stmt)).scalar_one_or_none()
            if order:
                for item in getattr(order, "items", []):
                    unit_price = getattr(item, "unit_price", 0)
                    qty = getattr(item, "quantity", 1)
                    line_total = Decimal(str(unit_price)) * qty
                    items.append({
                        "name": getattr(item, "product_title", getattr(item, "name", "Product Item")),
                        "quantity": qty,
                        "unit_price": format_naira(unit_price, is_kobo=False),
                        "total": format_naira(line_total, is_kobo=False),
                    })
                subtotal_kobo = int(Decimal(str(getattr(order, "subtotal_amount", 0))) * 100)
                shipping_kobo = int(Decimal(str(getattr(order, "shipping_amount", 0))) * 100)

        # Fallback if no item details
        if not items:
            items.append({
                "name": "Order Payment Checkout",
                "quantity": 1,
                "unit_price": format_naira(payment.amount, is_kobo=True),
                "total": format_naira(payment.amount, is_kobo=True),
            })
            subtotal_kobo = payment.amount

        vat_kobo = int(Decimal(str(subtotal_kobo)) * Decimal("0.075"))

        return {
            "invoice_number": f"HM-INV-{str(payment.id)[:8].upper()}",
            "receipt_number": f"HM-REC-{str(payment.id)[:8].upper()}",
            "transaction_ref": payment.transaction_ref,
            "provider": payment.provider.value.upper() if payment.provider else "PAYSTACK",
            "payment_status": payment.status.value.upper() if payment.status else "SUCCESSFUL",
            "paid_at": payment.paid_at.strftime("%d %b %Y, %I:%M %p") if payment.paid_at else "Confirmed",
            "customer": {
                "name": customer_name,
                "email": customer_email,
                "phone": customer_phone,
            },
            "items": items,
            "subtotal": format_naira(subtotal_kobo, is_kobo=True),
            "shipping_fee": format_naira(shipping_kobo, is_kobo=True),
            "vat": format_naira(vat_kobo, is_kobo=True),
            "total_paid": format_naira(payment.amount, is_kobo=True),
            "company": {
                "name": "Hamads Marketplace Ltd",
                "tagline": "Multi-Vendor E-Commerce & Logistics",
                "address": "Kano & Abuja, Nigeria",
                "support_email": "support@hamads.shop",
                "tin": "TIN-24892019-0001",
            }
        }

    async def generate_receipt_html(self, payment_id: uuid.UUID) -> str:
        """Generates an elegant, A4 print-ready HTML invoice & receipt."""
        data = await self.get_receipt_data(payment_id)

        items_html = "".join([
            f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 12px 8px; font-weight: 500; color: #1e293b;">{item['name']}</td>
                <td style="padding: 12px 8px; text-align: center; color: #64748b;">{item['quantity']}</td>
                <td style="padding: 12px 8px; text-align: right; color: #64748b;">{item['unit_price']}</td>
                <td style="padding: 12px 8px; text-align: right; font-weight: 600; color: #0f172a;">{item['total']}</td>
            </tr>
            """
            for item in data['items']
        ])

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Receipt - {data['receipt_number']}</title>
    <style>
        @page {{ size: A4; margin: 15mm; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background: #f8fafc;
            color: #0f172a;
            margin: 0;
            padding: 20px;
        }}
        .invoice-card {{
            max-width: 800px;
            margin: 0 auto;
            background: #ffffff;
            padding: 40px;
            border-radius: 12px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.06);
            border: 1px solid #e2e8f0;
        }}
        .badge {{
            display: inline-block;
            background: #dcfce7;
            color: #15803d;
            font-size: 12px;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 6px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        @media print {{
            body {{ background: #ffffff; padding: 0; }}
            .invoice-card {{ box-shadow: none; border: none; padding: 0; max-width: 100%; }}
            .no-print {{ display: none !important; }}
        }}
    </style>
</head>
<body>
    <div class="no-print" style="max-width: 800px; margin: 0 auto 16px auto; display: flex; justify-content: flex-end; gap: 10px;">
        <button onclick="window.print()" style="background: #2563eb; color: #fff; border: none; padding: 10px 20px; border-radius: 8px; font-weight: 600; cursor: pointer;">
            🖨️ Print / Save PDF
        </button>
    </div>

    <div class="invoice-card">
        <!-- Header -->
        <div style="display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 2px solid #f1f5f9; padding-bottom: 24px;">
            <div>
                <h1 style="margin: 0; font-size: 26px; font-weight: 800; color: #1e3a8a; letter-spacing: -0.5px;">HAMADS MARKETPLACE</h1>
                <p style="margin: 4px 0 0 0; font-size: 13px; color: #64748b;">{data['company']['tagline']}</p>
                <p style="margin: 2px 0 0 0; font-size: 12px; color: #94a3b8;">{data['company']['address']} | {data['company']['support_email']}</p>
            </div>
            <div style="text-align: right;">
                <span class="badge">{data['payment_status']}</span>
                <h2 style="margin: 8px 0 2px 0; font-size: 16px; color: #334155;">TAX INVOICE / RECEIPT</h2>
                <p style="margin: 0; font-size: 13px; font-weight: 600; color: #0f172a;">{data['receipt_number']}</p>
                <p style="margin: 2px 0 0 0; font-size: 12px; color: #64748b;">Date: {data['paid_at']}</p>
            </div>
        </div>

        <!-- Billed to & Payment Info -->
        <div style="display: flex; justify-content: space-between; margin: 28px 0;">
            <div>
                <p style="margin: 0 0 6px 0; font-size: 11px; font-weight: 700; text-transform: uppercase; color: #94a3b8; letter-spacing: 0.5px;">BILLED TO</p>
                <h3 style="margin: 0; font-size: 15px; color: #0f172a;">{data['customer']['name']}</h3>
                <p style="margin: 3px 0 0 0; font-size: 13px; color: #475569;">Email: {data['customer']['email']}</p>
                <p style="margin: 2px 0 0 0; font-size: 13px; color: #475569;">Phone: {data['customer']['phone']}</p>
            </div>
            <div style="text-align: right;">
                <p style="margin: 0 0 6px 0; font-size: 11px; font-weight: 700; text-transform: uppercase; color: #94a3b8; letter-spacing: 0.5px;">PAYMENT DETAILS</p>
                <p style="margin: 0; font-size: 13px; color: #475569;"><strong>Channel:</strong> {data['provider']} Gateway</p>
                <p style="margin: 2px 0 0 0; font-size: 12px; color: #64748b; font-family: monospace;"><strong>Ref:</strong> {data['transaction_ref']}</p>
            </div>
        </div>

        <!-- Table -->
        <table style="width: 100%; border-collapse: collapse; margin: 24px 0 32px 0;">
            <thead>
                <tr style="background: #f8fafc; border-bottom: 2px solid #e2e8f0; font-size: 12px; text-transform: uppercase; color: #475569;">
                    <th style="padding: 10px 8px; text-align: left;">Item Description</th>
                    <th style="padding: 10px 8px; text-align: center;">Qty</th>
                    <th style="padding: 10px 8px; text-align: right;">Unit Price</th>
                    <th style="padding: 10px 8px; text-align: right;">Total (₦)</th>
                </tr>
            </thead>
            <tbody style="font-size: 13px;">
                {items_html}
            </tbody>
        </table>

        <!-- Totals -->
        <div style="display: flex; justify-content: flex-end;">
            <div style="width: 280px; font-size: 13px;">
                <div style="display: flex; justify-content: space-between; padding: 6px 0; color: #64748b;">
                    <span>Subtotal:</span>
                    <span style="font-weight: 600; color: #0f172a;">{data['subtotal']}</span>
                </div>
                <div style="display: flex; justify-content: space-between; padding: 6px 0; color: #64748b;">
                    <span>Delivery / Shipping:</span>
                    <span style="font-weight: 600; color: #0f172a;">{data['shipping_fee']}</span>
                </div>
                <div style="display: flex; justify-content: space-between; padding: 6px 0; color: #64748b;">
                    <span>VAT (7.5%):</span>
                    <span style="font-weight: 600; color: #0f172a;">{data['vat']}</span>
                </div>
                <div style="display: flex; justify-content: space-between; padding: 12px 0; margin-top: 8px; border-top: 2px solid #0f172a; font-size: 16px; font-weight: 800; color: #1e3a8a;">
                    <span>Total Paid:</span>
                    <span>{data['total_paid']}</span>
                </div>
            </div>
        </div>

        <!-- Footer -->
        <div style="margin-top: 48px; padding-top: 20px; border-top: 1px solid #e2e8f0; font-size: 11px; color: #94a3b8; text-align: center;">
            <p style="margin: 0;">This is an electronically generated and certified tax invoice issued by {data['company']['name']}.</p>
            <p style="margin: 4px 0 0 0;">Tax Identification Number: {data['company']['tin']} | All rights reserved.</p>
        </div>
    </div>
</body>
</html>
"""
