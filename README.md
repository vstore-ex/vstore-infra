# VStore Infrastructure (`vstore-infra`)

Docker Compose orchestration and CI/CD for the **VStore** platform.

---

## 1. Architecture

All services run in an isolated bridge network (`app_net`). External traffic enters exclusively via the **Edge Reverse Proxy** (`proxy`) on standard HTTP (`80`) and HTTPS (`443`) ports. Application services (`frontend`, `admin`) and data stores (`postgres`, `redis`, `backend`) are isolated from public host interfaces.

```
                        Public / Host Traffic
                         (Ports 80 and 443)
                                  │
                                  ▼
                 ┌───────────────────────────────────┐
                 │       Edge Gateway (proxy)        │ (Nginx 1.27, SSL termination)
                 └───────────────┬───┬───────────────┘
                                 │   │
        Host: ${FRONTEND_DOMAIN} │   │ Host: ${ADMIN_DOMAIN}
                                 │   │
                 ┌───────────────┘   └───────────────┐
                 ▼                                   ▼
          ┌─────────────┐                     ┌─────────────┐
          │  frontend   │ (:8080 internal)    │    admin    │ (:8080 internal)
          └──────┬──────┘                     └──────┬──────┘
                 │                                   │
                 └───────────────► /api/ ◄───────────┘ (Internal DNS: backend:8000)
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
| `proxy` | `nginx:1.27-alpine` | `0.0.0.0:80`, `0.0.0.0:443` | `GET /healthz` | 0.5 CPU, 256M |
| `certbot` | `certbot/certbot:v3.1.0` | — | on-boot renewal loop (1h interval) | 0.2 CPU, 128M |
| `frontend` | `ghcr.io/vstore-ex/vstore-front` | internal (`expose: 8080`) | `GET /healthz` | 0.5 CPU, 256M |
| `admin` | `ghcr.io/vstore-ex/vstore-admin` | internal (`expose: 8080`) | `GET /healthz` | 0.5 CPU, 256M |
| `backend` | `ghcr.io/vstore-ex/vstore` | `127.0.0.1:8080` | `GET /health` | 1.5 CPU, 1024M |
| `worker` | `ghcr.io/vstore-ex/vstore` | — | disabled (celery worker) | 1.0 CPU, 1024M |
| `postgres` | `postgres:17` | `127.0.0.1:5432` | `pg_isready` | 1.0 CPU, 1024M |
| `redis` | `redis:7.2-alpine` | `127.0.0.1:6379` | `redis-cli ping` | 0.5 CPU, 256M |

## 3. Quick Start

```bash
cp .env.example .env   # configure FRONTEND_DOMAIN, ADMIN_DOMAIN, CERTBOT_EMAIL, POSTGRES_PASSWORD, etc.
mkdir -p storage/media && sudo chown -R 10001:10001 storage   # appuser writable storage
docker compose pull
docker compose up -d
docker compose ps      # verify all services report healthy

# Initial TLS Certificate Acquisition (first run on production host):
./scripts/init-letsencrypt.sh
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

**Offline production host** (the VM is expected to be off at times): deploy jobs **fail** when the host is unreachable — that is intentional, no fake-green CD. On boot the containers come back automatically (`restart: unless-stopped`). If commits were merged while the host was off, re-apply them with Actions → Infra CI → **Run workflow** (or *Re-run failed jobs* of the failed run — note: a re-run reuses the originally resolved reusable-workflow version; prefer *Run workflow* after callee changes).

## 7. CI/CD

`.github/workflows/`:

* `ci.yml` — validates the compose file, then syncs config to the production host over SSH (`docker compose up -d --wait`, flock-serialized, git-based rollback of config).
* `build-and-push.yml` — **reusable** build job (`workflow_call`) used by all app repos: Buildx + GHA cache, Trivy scan, push to the caller's GHCR namespace, outputs the image digest.
* `deploy-service.yml` — **reusable** deploy job used by all app repos: refuses to run if the currently running container's image has no registry digest (`RepoDigests` — no valid rollback target), pulls the digest-pinned image, optionally runs Alembic migrations behind a `pg_dump` snapshot, `up -d --wait`, health-check gate, automated rollback to the previous image digest (image only — schema restore is manual, commands are printed by the job).

App repos call these via `uses: vstore-ex/vstore-infra/.github/workflows/<file>.yml@main` with `secrets: inherit`. Requires the org setting *Actions → Allow reusable workflows from private repositories* for this org — without it every cross-repo call fails at workflow startup. This setting is the single dependency of the whole deduplicated CI layout.

## 8. Production Host Layout

* Deploy directory: **`~/vstore`** (SSH user's home; override with the `DEPLOY_PATH` secret) — must be a git clone of this repo; the deploy does `git fetch origin main && git reset --hard origin/main` inside it.
* Env file: `~/vstore/.env` — must define all fail-fast variables (see §5) plus `AZURE_STORAGE_CONNECTION_STRING` if Blob storage is used. `DATABASE_URL` must reference the compose service name (`@postgres:5432/...`), never `127.0.0.1`/`localhost`, and a database name that actually exists on the `postgres_data` volume (`POSTGRES_DB` only applies at first-volume init).
* GHCR: the host must be authenticated (`docker login ghcr.io` with a PAT holding `read:packages`) to pull private images.
* Docker Compose **v2 plugin** (`docker compose ...`) is required on the host; the standalone legacy `docker-compose` is not used.

### Proxy bootstrap verification

The edge proxy builds from `config/nginx/Dockerfile`, which installs the OpenSSL CLI required for fallback certificates. Run `docker compose build proxy` before applying the stack. Individual entrypoint mounts preserve the official Nginx envsubst script; the renewal watchdog runs after template expansion.

Run `bash tests/smoke-proxy.sh` to build and start the hardened proxy with an empty certificate directory. The test checks certificate generation, template expansion, Nginx syntax, and HTTP health. Infra CD captures container health and logs before attempting a bounded config rollback.

### Azure DNS label with local admin

For a public storefront hostname and a local admin hostname, set `ADMIN_CERT_NAME` to the existing local certificate directory (for example `vstore.local`) and `CERTBOT_INCLUDE_ADMIN_DOMAIN=0`. Issue the public certificate before switching `FRONTEND_DOMAIN`, so the fallback generator does not create an unmanaged directory at the intended Certbot certificate path. The admin continues to use its self-signed certificate.
