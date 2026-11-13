import logging
from redis.exceptions import RedisError
from app.core.redis_client import redis_client

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 30
MAX_PER_HOUR = 4
HOUR_SECONDS = 3600


async def enforce_otp_rate_limit(identifier: str, channel: str):
    cooldown_key = f"otp_cooldown:{channel}:{identifier}"
    count_key = f"otp_count:{channel}:{identifier}"

    try:
        if await redis_client.exists(cooldown_key):
            ttl = await redis_client.ttl(cooldown_key)
            raise ValueError(f"Please wait {ttl} seconds before requesting another code")

        count = await redis_client.incr(count_key)
        if count == 1:
            await redis_client.expire(count_key, HOUR_SECONDS)
        if count > MAX_PER_HOUR:
            raise ValueError("Too many code requests this hour. Please try again later.")

        await redis_client.setex(cooldown_key, COOLDOWN_SECONDS, "1")
    except RedisError as exc:
        logger.warning("Redis unavailable for OTP rate limit; failing open: %s", exc)


LOGIN_MAX_FAILURES_PER_ACCOUNT = 5
LOGIN_MAX_FAILURES_PER_IP = 20   # higher: many people can share one IP
LOGIN_LOCKOUT_SECONDS = 15 * 60


def _login_key(scope: str, kind: str, value: str) -> str:
    return f"login_fail:{scope}:{kind}:{value}"


async def enforce_login_lockout(scope: str, account: str, ip: str) -> None:
    """Call BEFORE checking the password. Raises ValueError while locked out."""
    try:
        for kind, value, limit in (
            ("acct", account, LOGIN_MAX_FAILURES_PER_ACCOUNT),
            ("ip", ip, LOGIN_MAX_FAILURES_PER_IP),
        ):
            key = _login_key(scope, kind, value)
            count = await redis_client.get(key)
            if count and int(count) >= limit:
                ttl = await redis_client.ttl(key)
                minutes = max(1, -(-max(ttl, 0) // 60))  # round up
                raise ValueError(f"Too many failed attempts. Try again in {minutes} minute(s).")
    except RedisError as exc:
        logger.warning("Redis unavailable for login lockout check; failing open: %s", exc)


async def record_login_failure(scope: str, account: str, ip: str) -> None:
    try:
        for kind, value in (("acct", account), ("ip", ip)):
            key = _login_key(scope, kind, value)
            count = await redis_client.incr(key)
            if count == 1:
                await redis_client.expire(key, LOGIN_LOCKOUT_SECONDS)
    except RedisError as exc:
        logger.warning("Redis unavailable for recording login failure; skipping: %s", exc)


async def clear_login_failures(scope: str, account: str) -> None:
    """After a successful login, forgive the account's failures (the IP counter keeps counting)."""
    try:
        await redis_client.delete(_login_key(scope, "acct", account))
    except RedisError as exc:
        logger.warning("Redis unavailable for clearing login failures; skipping: %s", exc)