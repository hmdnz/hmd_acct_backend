"""Strips the ecommerce leftovers out of hmd_acct_backend and leaves a ledger-only accounting service.

Run it ONCE from the project root (the folder that contains app/ and alembic/), with the _additions/ folder
that ships next to this script copied into the same root:

    python cleanup_accounting.py

Commit your repo first so you can review the diff. After it runs, copy the new files from this package over
the project (see the guide, step 5), then rebuild the database with a fresh Alembic baseline.
"""
import ast
import shutil
from pathlib import Path

ROOT = Path(".")
APP = ROOT / "app"
ADD = Path(__file__).resolve().parent / "_additions"

# --- 1. things to delete outright -------------------------------------------------------------------------------
DELETE_DIRS = [
    "modules/admin", "modules/cart", "modules/catalog", "modules/delivery", "modules/fulfillment",
    "modules/notifications", "modules/orders", "modules/payments", "modules/reviews", "modules/wishlist",
    "workers",
]
DELETE_FILES = [
    "modules/auth/otp_service.py", "modules/auth/rate_limit.py", "modules/auth/seller_service.py",
    "modules/auth/service.py", "modules/auth/router.py",
    "modules/accounting/paystack.py",
    "core/celery_app.py", "core/redis_client.py", "core/s3_client.py",
]
STRAY_ROOT_FILES = ["python", "f6c8a3d1b904"]  # empty/accidental files seen in the zip

# --- 2. tables and functions that only make sense inside the ecommerce database ------------------------------------
MODEL_CLASSES_TO_DROP = {"PaymentTransaction", "RefundRecord", "VendorPayout", "VendorReserveHold"}
SERVICE_FUNCS_TO_DROP = {
    "post_order_payment", "post_escrow_release", "process_seller_order_delivery", "post_order_refund",
    "get_vendor_withdrawable_balance", "get_vendor_subledger_summary", "create_vendor_payout",
    "complete_vendor_payout", "ingest_ecommerce_webhook_event",
}
ROUTER_FUNCS_TO_DROP = {
    "ecommerce_bridge_webhook", "trigger_order_paid", "trigger_escrow_release", "trigger_refund",
    "admin_list_payouts", "admin_complete_payout", "seller_get_my_ledger", "seller_list_my_payouts",
    "seller_request_payout", "paystack_webhook",
}


def drop_top_level(path: Path, names: set[str]) -> None:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    spans = []
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            spans.append((start, node.end_lineno))
    for start, end in sorted(spans, reverse=True):
        del lines[start - 1:end]
    path.write_text("".join(lines), encoding="utf-8")
    print(f"  {path}: removed {len(spans)} definition(s)")


def filter_import(path: Path, module: str, drop: set[str]) -> None:
    """Remove names from `from <module> import ...` (deleting the whole line if nothing is left)."""
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    for node in sorted(
        [n for n in ast.parse(src).body if isinstance(n, ast.ImportFrom) and n.module == module],
        key=lambda n: n.lineno, reverse=True,
    ):
        keep = [a for a in node.names if a.name not in drop]
        if len(keep) == len(node.names):
            continue
        if keep:
            body = ",\n    ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in keep)
            new = [f"from {module} import (\n    {body},\n)\n"]
        else:
            new = []
        lines[node.lineno - 1:node.end_lineno] = new
    path.write_text("".join(lines), encoding="utf-8")


def main() -> None:
    if not APP.is_dir():
        raise SystemExit("Run this from the project root (the folder that contains app/).")

    print("1. deleting ecommerce modules and helpers")
    for d in DELETE_DIRS:
        shutil.rmtree(APP / d, ignore_errors=True)
    for f in DELETE_FILES:
        (APP / f).unlink(missing_ok=True)
    for f in STRAY_ROOT_FILES:
        p = ROOT / f
        if p.is_file():
            p.unlink()

    print("2. models.py: dropping tables that point at ecommerce tables")
    models = APP / "modules/accounting/models.py"
    drop_top_level(models, MODEL_CLASSES_TO_DROP)
    text = models.read_text(encoding="utf-8")
    text = text.replace('class SubLedgerType(str, enum.Enum):\n    VENDOR = "VENDOR"\n',
                        'class SubLedgerType(str, enum.Enum):\n    VENDOR = "VENDOR"\n    RIDER = "RIDER"\n', 1)
    text = text.replace('    OPENING_BALANCE = "OPENING_BALANCE"\n',
                        '    OPENING_BALANCE = "OPENING_BALANCE"\n    RIDER_EARNING = "RIDER_EARNING"\n    RIDER_PAYOUT = "RIDER_PAYOUT"\n', 1)
    text = text.rstrip("\n") + "\n" + (ADD / "models_additions.py").read_text(encoding="utf-8")
    models.write_text(text, encoding="utf-8")

    print("3. service.py: dropping order/escrow/payout functions, adding ledger-only vendor summary")
    service = APP / "modules/accounting/service.py"
    drop_top_level(service, SERVICE_FUNCS_TO_DROP)
    text = service.read_text(encoding="utf-8")
    start = text.find("    payout_stmt = select(func.coalesce(func.sum(VendorPayout.amount), 0))")
    end = text.find("pending_payouts = _quantize(", start)
    if start != -1 and end != -1:
        end = text.find("\n", end) + 1
        text = text[:start] + '    pending_payouts = Decimal("0.0000")  # payouts are executed by hamads-dev, not here\n' + text[end:]
    service.write_text(text, encoding="utf-8")
    filter_import(service, "app.modules.accounting.models",
                  {"PaymentTransaction", "RefundRecord", "VendorPayout", "VendorReserveHold"})
    filter_import(service, "app.modules.auth.models", {"Seller"})
    service.write_text(
        service.read_text(encoding="utf-8").rstrip("\n") + "\n" + (ADD / "service_additions.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    print("4. router.py: dropping seller, payout, order-event and Paystack endpoints")
    router = APP / "modules/accounting/router.py"
    drop_top_level(router, ROUTER_FUNCS_TO_DROP)
    filter_import(router, "app.modules.accounting", {"paystack"})
    filter_import(router, "app.modules.accounting.models",
                  {"PaymentTransaction", "RefundRecord", "VendorPayout", "VendorReserveHold"})
    filter_import(router, "app.modules.auth.dependencies", {"get_current_seller"})
    filter_import(router, "app.modules.auth.models", {"Seller"})

    print("5. alembic: removing the old ecommerce migration chain (you will create one clean baseline)")
    versions = ROOT / "alembic/versions"
    if versions.is_dir():
        for f in versions.glob("*.py"):
            f.unlink()

    print("\nDone. Next: copy the new files from the package over this project, then follow the guide from step 6.")


if __name__ == "__main__":
    main()
