try:
    import redis.asyncio as redis
    from app.core.config import settings
    redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
except Exception:
    class DummyRedis:
        async def get(self, *args, **kwargs): return None
        async def set(self, *args, **kwargs): return True
        async def delete(self, *args, **kwargs): return True
        async def setex(self, *args, **kwargs): return True
        async def ping(self): return True
    redis_client = DummyRedis()