import redis

from app.core.config import settings


def _make_client(db: int) -> redis.Redis:
    return redis.from_url(settings.CELERY_BROKER_URL, db=db)


# each uses its own logical db so they dont share keyspace
redis_cache = _make_client(db=1)
redis_rate_limit = _make_client(db=2)
redis_blacklist = _make_client(db=3)
redis_refresh = _make_client(db=4)
redis_presence = _make_client(db=5)
redis_lock_client = _make_client(db=7)
redis_notifications = _make_client(db=8)
redis_views = _make_client(db=9)
