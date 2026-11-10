from sqlalchemy import Column, MetaData
import sqlalchemy.orm

if not hasattr(sqlalchemy.orm, "mapped_column"):
    def mapped_column(*args, **kwargs):
        return Column(*args, **kwargs)
    sqlalchemy.orm.mapped_column = mapped_column

try:
    from sqlalchemy.orm import DeclarativeBase
except ImportError:
    from sqlalchemy.orm import as_declarative

    @as_declarative()
    class DeclarativeBase:
        pass


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    __abstract__ = True
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
