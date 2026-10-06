import logging
import secrets

import redis

from app.core.redis import redis_refresh

logger = logging.getLogger(__name__)

# TTL refresh token: 3 days
REFRESH_TOKEN_EXPIRE_SECONDS = 3 * 24 * 60 * 60


def _decode(value) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


class RefreshTokenService:
    def __init__(self):
        # alias to the shared client; exposed so tests can patch `refresh_token_service.redis`
        self.redis = redis_refresh

    def create_refresh_token(self, user_id: int, device_id: str = "default") -> str:
        raw_token = secrets.token_urlsafe(32)
        user_hash_key = f"user:{user_id}:refresh_tokens"
        token_key = f"refresh_token:{raw_token}"

        try:
            old_token = self.redis.hget(user_hash_key, device_id)
            if old_token:
                self.redis.delete(f"refresh_token:{_decode(old_token)}")

            self.redis.hset(user_hash_key, device_id, raw_token)
            self.redis.set(token_key, f"{user_id}:{device_id}", ex=REFRESH_TOKEN_EXPIRE_SECONDS)
            return raw_token
        except redis.RedisError as exc:
            logger.error("Failed to store refresh token in Redis: %s", exc)
            return raw_token

    def verify_and_rotate_refresh_token(self, token: str) -> tuple[int, str, str] | None:
        token_key = f"refresh_token:{token}"
        try:
            data = self.redis.get(token_key)
            if not data:
                return None

            user_id_str, device_id = _decode(data).split(":", 1)
            user_id = int(user_id_str)

            self.redis.delete(token_key)

            new_refresh_token = self.create_refresh_token(user_id=user_id, device_id=device_id)
            return user_id, device_id, new_refresh_token
        except (redis.RedisError, ValueError) as exc:
            logger.error("failed to verify/rotate refresh token: %s", exc)
            return None

    def revoke_refresh_token(self, token: str) -> bool:
        token_key = f"refresh_token:{token}"
        try:
            data = self.redis.get(token_key)
            if not data:
                return False

            user_id_str, device_id = _decode(data).split(":", 1)
            user_id = int(user_id_str)

            self.redis.delete(token_key)
            self.redis.hdel(f"user:{user_id}:refresh_tokens", device_id)
            return True
        except redis.RedisError as exc:
            logger.error("failed to revoke refresh token: %s", exc)
            return False

    def revoke_all_user_tokens(self, user_id: int) -> bool:
        user_hash_key = f"user:{user_id}:refresh_tokens"
        try:
            tokens_dict = self.redis.hgetall(user_hash_key)
            for token in tokens_dict.values():
                self.redis.delete(f"refresh_token:{_decode(token)}")

            self.redis.delete(user_hash_key)
            return True
        except redis.RedisError as exc:
            logger.error("failed to revoke all user tokens: %s", exc)
            return False


refresh_token_service = RefreshTokenService()
