import logging

import redis

from app.core.redis import redis_blacklist

logger = logging.getLogger(__name__)


class TokenBlacklist:
    prefix = "blacklist:"

    def add(self, token: str, expire: int):
        try:
            redis_blacklist.set(f"{self.prefix}{token}", "1", ex=expire)
        except redis.RedisError as exc:
            logger.warning("Blacklist add failed: %s", exc)

    def is_blacklisted(self, token: str) -> bool:
        try:
            return redis_blacklist.exists(f"{self.prefix}{token}") > 0
        except redis.RedisError as exc:
            logger.warning("Blacklist check failed: %s", exc)
            return False


blacklist_service = TokenBlacklist()
