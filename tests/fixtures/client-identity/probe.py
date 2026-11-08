"""Exercise the unmodified production limiter without database/auth side effects."""

import importlib.util
import os
import sys
import types

import redis
from fastapi import Depends, FastAPI, Request

settings = types.SimpleNamespace(
    TRUST_PROXY_HEADERS=os.environ["TRUST_PROXY_HEADERS"].lower() == "true",
    RATE_LIMIT_ENABLED=True,
)
for name in ("app", "app.core", "app.core.config", "app.core.redis"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["app.core.config"].settings = settings
sys.modules["app.core.redis"].redis_rate_limit = redis.Redis(host="redis", db=2)
spec = importlib.util.spec_from_file_location("rate_limit", "rate_limit.py")
limiter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(limiter)

app = FastAPI()


@app.get("/health")
def health():
    sys.modules["app.core.redis"].redis_rate_limit.ping()
    return {"status": "ok"}


@app.get("/api/identity")
def identity(request: Request):
    return {
        "ip": limiter._client_ip(request),
        "scheme": request.url.scheme,
        "xff": request.headers.get("x-forwarded-for"),
        "real_ip": request.headers.get("x-real-ip"),
        "forwarded": request.headers.get("forwarded"),
    }


for path, count, window in (
    ("/api/v1/auth/login", 5, 60),
    ("/api/v1/auth/register", 5, 3600),
    ("/api/v1/media/upload", 20, 60),
):
    app.add_api_route(path, identity, methods=["POST"], dependencies=[Depends(limiter.limit_requests(count, window))])


@app.get("/api/slash/")
def slash():
    return {"ok": True}
