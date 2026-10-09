import asyncio
import logging
import time
import uuid

import redis

from app.core.redis import redis_lock_client

logger = logging.getLogger(__name__)

# Lua script to release lock only if the token matches
RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# Lua script to extend lock TTL only if the token matches
EXTEND_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
else
    return 0
end
"""


class LockError(Exception):
    def __init__(
        self,
        message: str = "Could not acquire distributed lock",
        key: str | None = None,
        timeout: float | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.key = key
        self.timeout = timeout

    def __str__(self) -> str:
        details = []
        if self.key:
            details.append(f"key='{self.key}'")
        if self.timeout is not None:
            details.append(f"timeout={self.timeout}s")
        if details:
            return f"{self.message} ({', '.join(details)})"
        return self.message


class RedisLock:
    def __init__(
        self,
        redis_client: redis.Redis,
        key: str,
        ttl: int = 15,
        timeout: float = 0.0,
        retry_interval: float = 0.05,
        fail_open: bool = False,
    ):
        self.redis = redis_client
        self.key = f"lock:{key}" if not key.startswith("lock:") else key
        self.ttl = ttl
        self.timeout = timeout
        self.retry_interval = retry_interval
        self.fail_open = fail_open
        self.token: str | None = None

    def acquire(self, blocking: bool | None = None) -> bool:
        is_blocking = (self.timeout > 0) if blocking is None else blocking
        token = uuid.uuid4().hex
        start_time = time.monotonic()

        while True:
            try:
                # SET key token NX EX ttl
                acquired = bool(self.redis.set(self.key, token, nx=True, ex=self.ttl))
                if acquired:
                    self.token = token
                    return True
            except redis.RedisError as exc:
                logger.error("Redis error acquiring lock for key=%s: %s", self.key, exc)
                if self.fail_open:
                    self.token = token
                    return True
                return False

            if not is_blocking:
                return False

            if time.monotonic() - start_time >= self.timeout:
                return False

            time.sleep(self.retry_interval)

    def release(self) -> bool:
        if not self.token:
            return False

        token = self.token
        self.token = None

        try:
            result = self.redis.eval(RELEASE_LUA, 1, self.key, token)
            return bool(result)
        except redis.RedisError as exc:
            logger.error("Redis error releasing lock for key=%s: %s", self.key, exc)
            return False

    def extend(self, additional_ttl: int | None = None) -> bool:
        if not self.token:
            return False

        ext_ttl = additional_ttl or self.ttl
        try:
            result = self.redis.eval(EXTEND_LUA, 1, self.key, self.token, ext_ttl)
            return bool(result)
        except redis.RedisError as exc:
            logger.error("Redis error extending lock for key=%s: %s", self.key, exc)
            return False

    def is_locked(self) -> bool:
        try:
            return bool(self.redis.exists(self.key))
        except redis.RedisError as exc:
            logger.error("Redis error checking lock for key=%s: %s", self.key, exc)
            return False

    def __enter__(self):
        if not self.acquire():
            raise LockError(
                f"Could not acquire lock for {self.key}",
                key=self.key,
                timeout=self.timeout,
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    async def __aenter__(self):
        token = uuid.uuid4().hex
        start_time = time.monotonic()
        is_blocking = self.timeout > 0

        while True:
            try:
                acquired = bool(self.redis.set(self.key, token, nx=True, ex=self.ttl))
                if acquired:
                    self.token = token
                    return self
            except redis.RedisError as exc:
                logger.error("Redis error acquiring async lock for key=%s: %s", self.key, exc)
                if self.fail_open:
                    self.token = token
                    return self
                raise LockError(
                    f"Redis error acquiring lock for {self.key}",
                    key=self.key,
                    timeout=self.timeout,
                ) from exc

            if not is_blocking or (time.monotonic() - start_time >= self.timeout):
                raise LockError(
                    f"Could not acquire lock for {self.key}",
                    key=self.key,
                    timeout=self.timeout,
                )

            await asyncio.sleep(self.retry_interval)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()


class LockService:
    def __init__(self, redis_client: redis.Redis = redis_lock_client):
        self.redis = redis_client

    def lock(
        self,
        key: str,
        ttl: int = 15,
        timeout: float = 0.0,
        retry_interval: float = 0.05,
        fail_open: bool = False,
    ) -> RedisLock:
        return RedisLock(
            redis_client=self.redis,
            key=key,
            ttl=ttl,
            timeout=timeout,
            retry_interval=retry_interval,
            fail_open=fail_open,
        )


lock_service = LockService()
