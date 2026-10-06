# VStore Infrastructure (`vstore-infra`)

Docker Compose orchestration and CI/CD for the **VStore** platform.

[Владельцы компонентов и порядок выбора reviewer](https://github.com/vstore-ex/Docs/blob/main/CONTRIBUTING.md).

---

## 1. Architecture

External traffic enters exclusively via the **Edge Reverse Proxy** (`proxy`) on standard HTTP (`80`) and HTTPS (`443`) ports. Edge and app proxies use `proxy_net`; backend also joins the existing data network `app_net`. Application services (`frontend`, `admin`) and data stores (`postgres`, `redis`, `backend`) are isolated from public host interfaces.

```
                        Public / Host Traffic
                         (Ports 80 and 443)
                                  │
                                  ▼
                 ┌───────────────────────────────────┐
                 │       Edge Gateway (proxy)        │ (Nginx stable, SSL termination)
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
| `proxy` | `ghcr.io/vstore-ex/vstore-infra-proxy` | `0.0.0.0:80`, `0.0.0.0:443` | `GET /healthz` | 0.5 CPU, 256M |
| `certbot` | `certbot/certbot:v3.1.0` | — | on-boot renewal loop (1h interval) | 0.2 CPU, 128M |
| `frontend` | `ghcr.io/vstore-ex/vstore-front` | `127.0.0.1:3000` (container `8080`) | `GET /healthz` | 0.5 CPU, 256M |
| `admin` | `ghcr.io/vstore-ex/vstore-admin` | `127.0.0.1:3001` (container `8080`) | `GET /healthz` | 0.5 CPU, 256M |
| `backend` | `ghcr.io/vstore-ex/vstore` | `127.0.0.1:8080` | `GET /health` | 1.5 CPU, 1024M |
| `worker` | `ghcr.io/vstore-ex/vstore` | — | disabled (celery worker) | 1.0 CPU, 1024M |
| `postgres` | `postgres:17` | `127.0.0.1:5432` | `pg_isready` | 1.0 CPU, 1024M |
| `redis` | `redis:7.2-alpine` | `127.0.0.1:6379` | `redis-cli ping` | 0.5 CPU, 256M |

## 3. Quick Start

```bash
cp .env.example .env   # configure FRONTEND_DOMAIN, ADMIN_DOMAIN, CERTBOT_EMAIL, POSTGRES_PASSWORD, etc.
mkdir -p storage/media && sudo chown -R 10001:10001 storage   # appuser writable storage
# For first installation, set all four *_IMAGE variables to reviewed registry digests.
# PROXY_IMAGE must come from a successful Infra CI security gate.
docker compose pull
docker compose up -d --wait
bash scripts/compose.sh bootstrap --proxy-image "$PROXY_IMAGE"  # export the reviewed digest first
bash scripts/compose.sh ps      # verify all services report healthy

# Initial TLS Certificate Acquisition (first run on production host):
# Requires Linux flock; HTTPS verification runs after certificate issuance.
./scripts/init-letsencrypt.sh
```

Generate secrets with `openssl rand -hex 32`.

## 4. Image Pinning

Verified current and previous application and edge digests live together in the host-local
`docker-compose.override.yml`. This generated JSON document is also valid YAML;
Compose automatically loads it in a fresh session. Its literal image references
override stale `.env` pins and exported `*_IMAGE` values. Do not commit or edit it.

Application CD loads the successful release under `.deploy.lock`, tests the
candidate using the base Compose file and exported digests, then atomically
publishes the override only after Compose, HTTP and running-image checks pass.
Infra CD preserves successful application images and tests its scanned edge candidate.
Manual commands use all successful images. Intentional rollback
also publishes its verified target, so later infra deployment preserves it.

Use `bash scripts/compose.sh` for manual operations to share the deployment lock.
See [release adoption and recovery](docs/releases.md) before rolling out this change.
The production host now requires Python 3 in addition to Bash, flock and Compose v2.

## 5. Hardening

Client identity is resolved through explicit trusted proxy addresses. The edge
replaces incoming XFF, Uvicorn trusts only the edge/frontend/admin addresses, and
backend raw-XFF trust is disabled in Compose. See [trust chain and backend handoff](https://github.com/vstore-ex/Docs/blob/main/infra/client-identity.md)
for subnet configuration, rollout and real-Redis regression tests.

1. **Network**: only the edge proxy is publicly bound (`80`/`443`). Frontend and admin publish `3000`/`3001` on `127.0.0.1` for CI deployment health checks; all other services stay on `127.0.0.1` or `app_net`.
2. **Containers**: app services (`backend`, `worker`, `frontend`, `admin`) run with `cap_drop: [ALL]` and `no-new-privileges`.
3. **Fail-fast env**: `POSTGRES_PASSWORD`, `DATABASE_URL`, `SECRET_KEY`, `ADMIN_*` have no defaults — a missing variable aborts `docker compose up` instead of silently deploying weak credentials.
4. **Logs**: `json-file` rotation (`10m` × `3`) via the `x-logging` anchor.
5. **Resources**: CPU/memory limits on every service.

## 6. Runbook

```bash
bash scripts/compose.sh logs -f worker     # follow worker logs
bash scripts/compose.sh down               # graceful stop (add -v to wipe data — DESTRUCTIVE)
bash scripts/compose.sh restart backend    # restart one service
docker compose config --quiet     # validate config before PRs
```

**Offline production host** (the VM is expected to be off at times): deploy jobs **fail** when the host is unreachable — that is intentional, no fake-green CD. On boot the containers come back automatically (`restart: unless-stopped`). If commits were merged while the host was off, re-apply them with Actions → Infra CI → **Run workflow** (or *Re-run failed jobs* of the failed run — note: a re-run reuses the originally resolved reusable-workflow version; prefer *Run workflow* after callee changes).

## 7. CI/CD

`.github/workflows/`:

* `ci.yml` — validates Compose, builds and scans the fresh edge runtime, and publishes that exact artifact to GHCR on `main`. SSH deployment pulls its immutable digest, preserves application pins, recreates the edge to render templates, and records successful state only after HTTPS API and image verification. Failure restores config and the previous edge artifact without rebuilding.
* `build-and-push.yml` — **reusable** build job (`workflow_call`) used by all app repos: Buildx + GHA cache, Trivy scan, push to the caller's GHCR namespace, outputs the image digest.
* `deploy-service.yml` — **reusable** deploy job used by all app repos: refuses to run if the currently running container's image has no registry digest (`RepoDigests` — no valid rollback target), pulls the digest-pinned image, optionally runs Alembic migrations behind a `pg_dump` snapshot, `up -d --wait`, health-check gate, automated rollback to the previous image digest (image only — schema restore is manual, commands are printed by the job).

App repos call these via `uses: vstore-ex/vstore-infra/.github/workflows/<file>.yml@main` with `secrets: inherit`. Requires the org setting *Actions → Allow reusable workflows from private repositories* for this org — without it every cross-repo call fails at workflow startup. This setting is the single dependency of the whole deduplicated CI layout.

## 8. Production Host Layout

* Deploy directory: **`~/vstore`** (SSH user's home; override with the `DEPLOY_PATH` secret) — must be a git clone of this repo; the deploy does `git fetch origin main && git reset --hard origin/main` inside it.
* Deployment permissions: the SSH user owns the private `0700` clone, `.env`/release state/snapshots use `0600`, and backups use `0700`. Run `python3 scripts/secure-deployment.py` before adoption; both SSH workflows enforce the policy with `umask 077`. Preserve application-owned storage separately. See [private deployment permissions](https://github.com/vstore-ex/Docs/blob/main/infra/deployment-permissions.md).
* Env file: `~/vstore/.env` — must define all fail-fast variables (see §5) plus `AZURE_STORAGE_CONNECTION_STRING` if Blob storage is used. `DATABASE_URL` must reference the compose service name (`@postgres:5432/...`), never `127.0.0.1`/`localhost`, and a database name that actually exists on the `postgres_data` volume (`POSTGRES_DB` only applies at first-volume init).
* GHCR: the host must be authenticated (`docker login ghcr.io` with a PAT holding `read:packages`) to pull private images.
* Docker Compose **v2 plugin** (`docker compose ...`) is required on the host; the standalone legacy `docker-compose` is not used.

### Proxy bootstrap verification

Infra CI builds the edge from `config/nginx/Dockerfile`, refreshes the Nginx stable base and runtime packages, and installs the OpenSSL CLI required for fallback certificates. The host pulls the scanned GHCR digest; Compose does not build the edge. Individual entrypoint mounts preserve the official Nginx envsubst script; the renewal watchdog runs after template expansion.

Run `bash tests/smoke-proxy.sh` to build and start the hardened proxy with an empty certificate directory, or supply an image ID/digest to test that exact artifact. The test checks certificate generation, template expansion, Nginx syntax, and HTTP health. Infra CD captures container health and logs before attempting a bounded config/artifact rollback. See [edge security gate and adoption](https://github.com/vstore-ex/Docs/blob/main/infra/edge-security.md).

### Azure DNS label with local admin

For a public storefront hostname and a local admin hostname, set `ADMIN_CERT_NAME` to the existing local certificate directory (for example `vstore.local`) and `CERTBOT_INCLUDE_ADMIN_DOMAIN=0`. Issue the public certificate before switching `FRONTEND_DOMAIN`, so the fallback generator does not create an unmanaged directory at the intended Certbot certificate path. The admin continues to use its self-signed certificate.


## Commit messages

Run `npm ci` with Node.js 22.12+ to enable the local Conventional Commits
check. Pull requests also validate commits and the PR title in GitHub Actions.
See [commit message guidelines](docs/commit-messages.md) for examples and setup.

Deployment recovery reloads dependent Nginx proxies and checks both HTTPS API routes before publishing release state. Use `bash scripts/compose.sh recover` after out-of-band recreation. See [upstream recovery](https://github.com/vstore-ex/Docs/blob/main/infra/upstream-recovery.md).

Database snapshot recovery requires stopped writers, a new target and explicit data/revision verification. See [manual database recovery](https://github.com/vstore-ex/Docs/blob/main/infra/database-recovery.md). Automatic rollback remains image-only.

Off-host recovery bundles use encrypted restic in a dedicated private Azure container. The optional host timer checks on boot and retries every 15 minutes; it creates a copy only when the last completed off-host recovery point is at least 168 hours old. An interrupted/partial upload cannot postpone retry. Installation, four-week retention proposal, credential escrow and isolated replacement recovery are described in [off-host backups](https://github.com/vstore-ex/Docs/blob/main/infra/offhost-backups.md). The timer is not installed or enabled by deployment; approve storage costs and the writer maintenance window before adoption.
