

# ---------------------------------------------------------------------------
# Added by cleanup_accounting.py: name directory for sellers and riders sent by hamads-dev
# ---------------------------------------------------------------------------
from sqlalchemy import UniqueConstraint  # noqa: E402


class Party(Base, UUIDMixin, TimestampMixin):
    """The ledger only stores ids on journal lines. This table remembers the matching names."""
    __tablename__ = "accounting_parties"

    party_type: Mapped[str] = mapped_column(String(20), index=True)   # "seller" | "rider"
    external_id: Mapped[str] = mapped_column(String(64), index=True)  # the id used in hamads-dev
    name: Mapped[str] = mapped_column(String(200))

    __table_args__ = (UniqueConstraint("party_type", "external_id"),)
