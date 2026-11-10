# App initialization and SQLAlchemy compatibility shim
import sqlalchemy.orm
from sqlalchemy import Column

if not hasattr(sqlalchemy.orm, "mapped_column"):
    def _mapped_column(*args, **kwargs):
        return Column(*args, **kwargs)
    sqlalchemy.orm.mapped_column = _mapped_column

try:
    from sqlalchemy.orm import DeclarativeBase
except ImportError:
    from sqlalchemy.orm import as_declarative
    @as_declarative()
    class DeclarativeBase:
        pass
    sqlalchemy.orm.DeclarativeBase = DeclarativeBase
