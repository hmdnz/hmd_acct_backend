"""snapshot shipping address on orders

Revision ID: 75a0ef29cb26
Revises: c4787d7be1dd
Create Date: 2026-10-09 13:50:45.545020

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '75a0ef29cb26'
down_revision: Union[str, None] = 'c4787d7be1dd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


REQUIRED = [
    ("ship_recipient_name", sa.String(255)), ("ship_phone_number", sa.String(50)),
    ("ship_country", sa.String(100)), ("ship_state", sa.String(100)),
    ("ship_lga", sa.String(100)), ("ship_street", sa.String(255)),
    ("ship_house_number", sa.String(50)),
]


def upgrade() -> None:
    for name, type_ in REQUIRED:
        op.add_column("orders", sa.Column(name, type_, nullable=True))
    op.add_column("orders", sa.Column("ship_landmark", sa.String(255), nullable=True))
    op.add_column("orders", sa.Column("ship_latitude", sa.Float(), nullable=True))
    op.add_column("orders", sa.Column("ship_longitude", sa.Float(), nullable=True))

    # Backfill existing orders from the address they point at and the account holder.
    op.execute("""
        UPDATE orders o SET
            ship_recipient_name = trim(concat_ws(' ', u.first_name, u.last_name)),
            ship_phone_number   = u.phone_number,
            ship_country        = a.country,
            ship_state          = a.state,
            ship_lga            = a.lga,
            ship_street         = a.street,
            ship_house_number   = a.house_number,
            ship_landmark       = a.landmark,
            ship_latitude       = a.latitude,
            ship_longitude      = a.longitude
        FROM users u, addresses a
        WHERE u.id = o.user_id AND a.id = o.address_id
    """)

    for name, type_ in REQUIRED:
        op.alter_column("orders", name, existing_type=type_, nullable=False)

    # address_id: nullable, and SET NULL when the address is deleted.
    op.drop_constraint("fk_orders_address_id_addresses", "orders", type_="foreignkey")
    op.alter_column("orders", "address_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.create_foreign_key(
        op.f("fk_orders_address_id_addresses"), "orders", "addresses",
        ["address_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    for col in ["ship_longitude", "ship_latitude", "ship_landmark"] + [n for n, _ in REQUIRED]:
        op.drop_column("orders", col)
    # address_id is left nullable on purpose: making it NOT NULL again would fail
    # for any order whose saved address was deleted after this migration.
