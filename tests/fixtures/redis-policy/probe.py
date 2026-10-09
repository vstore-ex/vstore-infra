"""Run unchanged application snapshots against disposable Redis only."""

import asyncio
import importlib.util
import json
import logging
import os
import sys
import types
import time

import redis

logging.disable(logging.CRITICAL)  # never print synthetic refresh/access tokens
settings = types.SimpleNamespace(CELERY_BROKER_URL="redis://redis:6379/0",
                                 CELERY_RESULT_BACKEND="redis://redis:6379/0",
                                 RATE_LIMIT_ENABLED=True, TRUST_PROXY_HEADERS=False)
for name in ("app", "app.core", "app.core.config", "app.worker"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["app.core.config"].settings = settings


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


clients = load("app.core.redis", "client_factory.py")
blacklist = load("blacklist", "blacklist.py").blacklist_service
refresh = load("refresh", "refresh_token.py").refresh_token_service
limiter = load("limiter", "rate_limit.py").rate_limiter
cache = load("cache", "cache.py").cache_service
locks = load("locks", "lock.py").lock_service
celery = load("app.worker.celery_app", "celery_app.py").celery_app
celery.conf.update(broker_connection_timeout=1, broker_connection_retry=False,
                   task_publish_retry=False)
client = redis.Redis(host="redis", socket_connect_timeout=1, socket_timeout=1)


def seed():
    token = refresh.create_refresh_token(123, "a07-device")
    assert refresh.redis.exists("refresh_token:" + token)
    blacklist.add("a07-revoked", 3600)
    cache.set("a07-cache", {"ok": True}, expire=3600)
    client.set("a07-expire", "temporary", ex=3)
    client.set("a07-view-counter", 19)
    client.set("a07-live-token", token)  # fixture only; never print this key's value
    celery.send_task("a07.fixture", task_id="a07-default", queue="default")
    celery.send_task("a07.fixture", task_id="a07-media", queue="media")
    celery.backend.store_result("a07-result", {"ok": True}, "SUCCESS")
    return {"queued": 2, "dbs": {
        name: value.connection_pool.connection_kwargs["db"]
        for name, value in vars(clients).items() if isinstance(value, redis.Redis)}}


def verify():
    assert blacklist.is_blacklisted("a07-revoked")
    token = client.get("a07-live-token").decode()
    assert refresh.redis.exists("refresh_token:" + token)
    assert cache.get("a07-cache") == {"ok": True}
    assert client.get("a07-view-counter") == b"19"
    assert client.llen("default") == 1
    assert client.llen("media") == 0 and client.hlen("unacked") == 1
    assert celery.backend.get_task_meta("a07-result")["status"] == "SUCCESS"
    assert client.get("a07-expire") is None
    rotated = refresh.verify_and_rotate_refresh_token(token)
    assert rotated and refresh.redis.exists("refresh_token:" + rotated[2])
    client.set("a07-live-token", rotated[2])
    client.set("a07-fsync-barrier", "1")
    assert client.execute_command("WAITAOF", 1, 0, 5000)[0] == 1
    return {"auth_and_queues": "survive", "expired_key": "absent"}


def outage():
    # Select unreachable endpoint; exercise the actual client exception paths.
    dead = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.1,
                       socket_timeout=0.1)
    clients.redis_blacklist = dead
    sys.modules["blacklist"].redis_blacklist = dead
    refresh.redis = limiter.redis = dead
    sys.modules["cache"].redis_cache = dead
    assert not blacklist.is_blacklisted("a07-revoked")
    assert refresh.verify_and_rotate_refresh_token("missing") is None
    assert refresh.create_refresh_token(123)  # returned despite failed storage
    assert not refresh.revoke_refresh_token("missing")
    assert asyncio.run(limiter.check("a07-limit", 1, 60)) is None
    assert cache.get("missing") is None
    cache.set("a07-cache", {"ok": True})
    locks.redis = dead
    assert not locks.lock("a07-outage").acquire()
    assert locks.lock("a07-outage", fail_open=True).acquire()
    celery.conf.broker_url = "redis://127.0.0.1:1/0"
    try:
        celery.send_task("a07.fixture", queue="default")
    except Exception:
        broker = "raises"
    else:
        raise AssertionError("Broker publish must reject an unreachable Redis")
    return {"blacklist": "fail-open", "rate_limit": "fail-open",
            "refresh_lookup": "fail-closed", "refresh_create": "returns-unusable-token",
            "cache": "miss-or-skipped-write", "broker": broker,
            "locks": "default-fail-closed-or-explicit-fail-open"}


def pressure():
    # The cache shares DB0 in the current URL-based client factory.
    count = 0
    try:
        while count < 200:
            client.set("a07-fill:" + str(count), b"x" * (512 * 1024))
            count += 1
    except redis.exceptions.OutOfMemoryError:
        pass
    else:
        raise AssertionError("Expected deterministic noeviction OOM before 100 MiB")
    # A rejected 512 KiB request can leave room for small keys. Exhaust that
    # remainder with progressively smaller writes before measuring rewrite peaks.
    large_count = count
    for size in (4096, 256, 1):
        for _ in range(10000):
            try:
                client.set("a07-fill:" + str(count), b"x" * size)
                count += 1
            except redis.exceptions.OutOfMemoryError:
                break
        else:
            raise AssertionError("Unbounded pressure fixture")
    assert blacklist.is_blacklisted("a07-revoked")
    assert client.llen("default") == 1
    assert client.llen("media") == 0 and client.hlen("unacked") == 1
    assert client.info("stats")["evicted_keys"] == 0
    result = {"filled_keys": count, "evicted_keys": 0,
              "used_memory": client.info("memory")["used_memory"],
              "rejected_writes": "OOM"}
    # Bound and dirty an existing dataset during an actual AOF rewrite.
    client.delete(*(f"a07-fill:{index}" for index in range(large_count - 4, large_count)))
    client.bgrewriteaof()
    for index in range(min(large_count - 4, 90)):
        client.set("a07-fill:" + str(index), b"y" * (512 * 1024))
    return result


def oom_behavior():
    # Real Redis OOM, deterministically below the retained dataset. Testing each
    # tiny write at the 64 MiB boundary races connection/buffer reclamation.
    original = client.config_get("maxmemory")["maxmemory"]
    client.config_set("maxmemory", 1)
    try:
        assert asyncio.run(limiter.check("a07-new-counter", 1, 60)) is None
        blacklist.add("a07-new-revoked", 3600)
        assert not blacklist.is_blacklisted("a07-new-revoked")
        unusable = refresh.create_refresh_token(456)
        assert not refresh.redis.exists("refresh_token:" + unusable)
        cache.set("a07-new-cache", {"ok": True})
        assert cache.get("a07-new-cache") is None
        try:
            celery.send_task("a07.fixture", queue="default")
        except Exception:
            pass
        else:
            raise AssertionError("Broker publish must reject deterministic Redis OOM")
        return {"fixture_maxmemory_bytes": 1, "broker_publish": "raises",
                "blacklist_write": "silently-skipped", "rate_limit": "fail-open",
                "refresh_create": "returns-unusable-token", "cache_write": "skipped"}
    finally:
        client.config_set("maxmemory", original)


def cleanup():
    keys = list(client.scan_iter("a07-fill:*"))
    if keys:
        client.delete(*keys)
    return {"removed_fixture_keys": len(keys)}


def reserve():
    connection = celery.connection()
    queue = connection.SimpleQueue("media")
    message = queue.get(block=False)
    assert message.headers["id"] == "a07-media"
    assert client.hlen("unacked") == 1
    # Real process loss prevents Kombu's normal shutdown from requeuing the job.
    client.set("a07-reservation-barrier", "1")
    assert client.execute_command("WAITAOF", 1, 0, 5000)[0] == 1
    print(json.dumps({"reserved": 1, "acknowledged": 0}), flush=True)
    os._exit(0)


def redeliver():
    tags = client.zrange("unacked_index", 0, -1)
    assert len(tags) == 1
    # Advance only the disposable delivery's visibility deadline, without waiting an hour.
    client.zadd("unacked_index", {tags[0]: time.time() - 3605})
    with celery.connection() as connection:
        connection.channel().qos.restore_visible(interval=1)
    assert client.hlen("unacked") == 0 and client.llen("media") == 1
    return {"unacknowledged_delivery": "restored-after-visibility-timeout", "exactly_once": False}


def lost():
    assert not blacklist.is_blacklisted("a07-revoked")
    assert refresh.verify_and_rotate_refresh_token("missing") is None
    assert cache.get("a07-cache") is None
    assert client.llen("default") == client.llen("media") == 0
    assert asyncio.run(limiter.check("a07-limit", 1, 60)) is None
    return {"blacklist": "revoked-access-token-no-longer-blocked",
            "refresh": "requires-login", "broker": "pending-jobs-lost",
            "cache": "miss", "rate_limit": "counter-reset"}


operations = {"seed": seed, "verify": verify, "outage": outage,
              "pressure": pressure, "lost": lost, "cleanup": cleanup,
              "reserve": reserve, "redeliver": redeliver, "oom": oom_behavior}
print(json.dumps(operations[sys.argv[1]]()))
