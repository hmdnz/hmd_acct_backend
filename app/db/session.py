from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
try:
    from sqlalchemy.ext.asyncio import async_sessionmaker
except ImportError:
    from sqlalchemy.orm import sessionmaker

    def async_sessionmaker(bind=None, class_=AsyncSession, expire_on_commit=False, **kwargs):
        return sessionmaker(bind=bind, class_=class_, expire_on_commit=expire_on_commit, **kwargs)

from app.core.config import settings

engine = None
AsyncSessionLocal = None

try:
    engine = create_async_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        echo=settings.ENVIRONMENT == "development",
    )
    AsyncSessionLocal = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
except Exception:
    pass


async def get_db():
    global AsyncSessionLocal, engine
    if AsyncSessionLocal is None:
        if engine is None:
            engine = create_async_engine(
                settings.DATABASE_URL,
                pool_pre_ping=True,
                pool_size=10,
                max_overflow=20,
                echo=settings.ENVIRONMENT == "development",
            )
        AsyncSessionLocal = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )

    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
