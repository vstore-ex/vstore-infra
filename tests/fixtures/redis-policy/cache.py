import json
import logging
from typing import Any

from app.core.redis import redis_cache

logger = logging.getLogger(__name__)


def _default(obj):
    """JSON serializer for Decimal and datetime."""
    from datetime import datetime
    from decimal import Decimal

    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


class CacheService:
    def get(self, key: str) -> Any | None:
        try:
            data = redis_cache.get(key)
            if data:
                return json.loads(data)
        except Exception:
            logger.exception("cache get failed for key=%s", key)
        return None

    def set(self, key: str, value: Any, expire: int = 600):
        try:
            serialized_data = json.dumps(value, default=_default)
            redis_cache.set(key, serialized_data, ex=expire)
        except Exception:
            logger.exception("cache set failed for key=%s", key)

    def delete(self, key: str):
        try:
            redis_cache.delete(key)
        except Exception:
            logger.exception("cache delete failed for key=%s", key)


cache_service = CacheService()
