# VStore Infrastructure (`vstore-infra`)

Docker Compose orchestration and CI/CD for the **VStore** platform.

---

## 1. Architecture

All services run in an isolated bridge network (`app_net`). Reverse proxy (nginx) ships **inside** the `frontend` and `admin` images — this repo contains no nginx configs. PostgreSQL, Redis, and the backend bind strictly to `127.0.0.1`.

```
                        Public / Host Traffic
                         │                 │
           Storefront    │                 │  Admin Dashboard
           (Port 3000)   ▼                 ▼  (Port 3001)
                   ┌───────────┐     ┌───────────┐
                   │ frontend  │     │   admin   │ (nginx in-image, :8080)
                   └─────┬─────┘     └─────┬─────┘
                         │                 │
                         └───► /api/ ◄─────┘ (Internal DNS: backend:8000)
                                   │
                                   ▼
                         ┌───────────────────┐
                         │      backend      │ (FastAPI / Uvicorn)
                         └────┬────────┬─────┘
                              │        │
              DB Queries ┌────┘        └─────┐ Broker / Cache
                         ▼                   ▼
                  ┌────────────┐       ┌────────────┐
                  │  postgres  │       │   redis    │
                  └────────────┘       └─────▲──────┘
                                             │ Celery
                                      ┌──────┴─────┐
                                      │   worker   │
                                      └──────┬─────┘
                                             │
                                      ┌──────▼─────┐
                                      │ ./storage  │ (shared media)
                                      └────────────┘
```

## 2. Services

| Service | Image | Host Binding | Health Check | Limits |
| :--- | :--- | :--- | :--- | :--- |
| `frontend` | `ghcr.io/vstore-ex/vstore-front` | `0.0.0.0:3000` | `GET /healthz` | 0.5 CPU, 256M |
| `admin` | `ghcr.io/vstore-ex/vstore-admin` | `0.0.0.0:3001` | `GET /healthz` | 0.5 CPU, 256M |
| `backend` | `ghcr.io/vstore-ex/vstore` | `127.0.0.1:8080` | `GET /health` | 1.5 CPU, 1024M |
| `worker` | `ghcr.io/vstore-ex/vstore` | — | `depends_on` only | 1.0 CPU, 1024M |
| `postgres` | `postgres:17` | `127.0.0.1:5432` | `pg_isready` | 1.0 CPU, 1024M |
| `redis` | `redis:7.2-alpine` | `127.0.0.1:6379` | `redis-cli ping` | 0.5 CPU, 256M |

## 3. Quick Start

```bash
cp .env.example .env   # fill in secrets; POSTGRES_PASSWORD / SECRET_KEY / etc. are fail-fast
mkdir -p storage/media && chmod -R 775 storage   # backend runs as UID 10001
docker compose pull
docker compose up -d
docker compose ps      # wait until all services report healthy
```

Generate secrets with `openssl rand -hex 32`.

## 4. Image Pinning

`.env` defaults images to `:latest`, but each app repo's CI deploys its **exact digest** (`name@sha256:...`) by exporting the `*_IMAGE` variable over `.env` (shell environment wins in Compose interpolation). Rollback re-exports the previously running digest. Keep this mechanism if you change deploy tooling.

## 5. Hardening

1. **Network**: only `frontend`/`admin` are publicly bound (`3000`/`3001`); everything else stays on `127.0.0.1` or `app_net`.
2. **Containers**: app services (`backend`, `worker`, `frontend`, `admin`) run with `cap_drop: [ALL]` and `no-new-privileges`.
3. **Fail-fast env**: `POSTGRES_PASSWORD`, `DATABASE_URL`, `SECRET_KEY`, `ADMIN_*` have no defaults — a missing variable aborts `docker compose up` instead of silently deploying weak credentials.
4. **Logs**: `json-file` rotation (`10m` × `3`) via the `x-logging` anchor.
5. **Resources**: CPU/memory limits on every service.

## 6. Runbook

```bash
docker compose logs -f worker     # follow worker logs
docker compose down               # graceful stop (add -v to wipe data — DESTRUCTIVE)
docker compose restart backend    # restart one service
docker compose config --quiet     # validate config before PRs
```

## 7. CI/CD

`.github/workflows/`:

* `ci.yml` — validates the compose file, then syncs config to the production host over SSH (`docker compose up -d --wait`, flock-serialized, git-based rollback of config).
* `build-and-push.yml` — **reusable** build job (`workflow_call`) used by all app repos: Buildx + GHA cache, Trivy scan, push to the caller's GHCR namespace, outputs the image digest.
* `deploy-service.yml` — **reusable** deploy job used by all app repos: pulls the digest-pinned image, optionally runs Alembic migrations behind a `pg_dump` snapshot, `up -d --wait`, health-check gate, automated rollback to the previous image digest.

App repos call these via `uses: vstore-ex/vstore-infra/.github/workflows/<file>.yml@main` with `secrets: inherit`. Requires the org setting *Actions → Allow reusable workflows from private repositories* for this org.
