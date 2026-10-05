import logging

import redis
from fastapi import HTTPException, Request, status

from app.core.config import settings
from app.core.redis import redis_rate_limit

logger = logging.getLogger(__name__)


# RFC 6585 & IETF Draft: HTTP Rate Limiting Headers
# communicates request limits and cooldowns using standard headers (Retry-After, RateLimit-Limit, RateLimit-Remaining, RateLimit-Reset)
class RateLimiter:
    def __init__(self, redis_client: redis.Redis = redis_rate_limit):
        self.redis = redis_client

    async def check(self, key: str, limit: int, window: int) -> None:
        if not getattr(settings, "RATE_LIMIT_ENABLED", True):
            return

        try:
            count = self.redis.incr(key)
            if count == 1:
                self.redis.expire(key, window)
            else:
                ttl = self.redis.ttl(key)
                if ttl == -1:
                    self.redis.expire(key, window)

            if count > limit:
                ttl = self.redis.ttl(key)
                retry_after = max(ttl, 1) if ttl and ttl > 0 else window
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests. Please try again later.",
                    headers={
                        "Retry-After": str(retry_after),
                        "RateLimit-Limit": str(limit),
                        "RateLimit-Remaining": "0",
                        "RateLimit-Reset": str(retry_after),
                    },
                )
        except (redis.exceptions.ConnectionError, redis.exceptions.RedisError) as exc:
            logger.warning("Redis rate limit check failed (failing open): %s", exc)


rate_limiter = RateLimiter()


async def check_rate_limit(key: str, limit: int, window: int) -> None:
    await rate_limiter.check(key, limit, window)


def _client_ip(request: Request) -> str:
    if getattr(settings, "TRUST_PROXY_HEADERS", False):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


def limit_requests(requests: int, window: int):
    async def dependency(request: Request):
        client_ip = _client_ip(request)
        key = f"rate_limit:{request.url.path}:{client_ip}"
        await check_rate_limit(key, requests, window)

    return dependency
