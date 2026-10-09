# VStore Infrastructure (`vstore-infra`) - Agent Guidelines & Runbook

Conventions, constraints, and verification procedures for agents and DevOps engineers working in **`vstore-infra`**.

---

## 1. Repository Scope

`vstore-infra` owns:
- **Docker Compose** orchestration (`docker-compose.yml`) — the full production stack.
- **Shared CI/CD**: reusable workflows (`build-and-push.yml`, `deploy-service.yml`) that every app repo calls for image builds and SSH deploys.
- **Infra CI** (`ci.yml`): Compose validation, fresh edge build/scan/publish, and config plus scanned-digest deployment to the production host.

Reverse proxy (nginx) lives **inside the `frontend`/`admin` images** — edge TLS proxy configs live in `config/nginx`; application proxy configs remain in the app repos. Media routing, CSP, and proxy headers belong to those repos.

## 2. Service Topology

| Service | Image | Host Binding | Health Check |
| :--- | :--- | :--- | :--- |
| `postgres` | `postgres:17` | `127.0.0.1:5432` | `pg_isready` |
| `redis` | `redis:7.2-alpine` | `127.0.0.1:6379` | `redis-cli ping` |
| `backend` | `ghcr.io/vstore-ex/vstore` | `127.0.0.1:8080` | `GET /health` |
| `worker` | `ghcr.io/vstore-ex/vstore` | — | healthcheck **disabled** (celery serves no HTTP; the image-inherited HEALTHCHECK probes `:8000` and would permanently report unhealthy) |
| `frontend` | `ghcr.io/vstore-ex/vstore-front` | `0.0.0.0:3000` | `GET /healthz` |
| `admin` | `ghcr.io/vstore-ex/vstore-admin` | `0.0.0.0:3001` | `GET /healthz` |

## 3. Strict Rules

### 🔴 1. Git Workflow: PRs Only
- Never commit directly to `main`. Branch (`feat/*`, `fix/*`, `chore/*`), submit a PR.

### 🔒 2. Port Binding & Network Security
- `backend`, `postgres`, `redis` bind strictly to `127.0.0.1` (or stay unexposed).
- Only `frontend` (`3000`) and `admin` (`3001`) may bind `0.0.0.0`.

### 🔒 3. Fail-Fast Secrets
- `POSTGRES_PASSWORD`, `DATABASE_URL`, `SECRET_KEY`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` use `:?` interpolation — no defaults. Never add weak defaults back.

### 🔒 4. Container Hardening
- App services carry `cap_drop: [ALL]` + `no-new-privileges` (see `x-hardening` anchor). New app services must include the anchor. PostgreSQL/Redis are exempt (privileged init).

### 🔒 Trusted Client Identity
- Edge overwrites XFF with its TCP peer. Backend raw-XFF trust must stay disabled; Uvicorn trusts only the exact edge/frontend/admin addresses on `proxy_net`. Never use wildcard/subnet proxy trust. Backend also joins `app_net`; the gateway and data services are not trusted hops. See [trusted client identity](https://github.com/vstore-ex/Docs/blob/main/infra/client-identity.md) and its backend handoff.

### 🔄 5. Rollback Preflight & Image Pinning
- App deploys load verified digests from the host-local `docker-compose.override.yml` under the shared flock, then test candidates with `release_compose` (base file plus exported `*_IMAGE`). Never rely on `:latest` for a deploy or a rollback. Publish current and previous digests atomically only after health and image verification; see `docs/releases.md`.
- The deploy preflight refuses to run if the current container's image has no `RepoDigests` (no valid rollback target). `RepoDigests` lives on the **image** — it must be read via two-step inspect (container `.Image` → image). Do not "simplify" this back to a single inspect.
- Rollback restores the container image only. DB schema is never downgraded automatically; the deploy job links the [verified manual database recovery procedure](https://github.com/vstore-ex/Docs/blob/main/infra/database-recovery.md). Restore only into a new database with stopped writers; automatic destructive restore and unreviewed downgrade hints are prohibited.

### Redis state (A07)
- Redis shares broker/auth/cache state. Keep `noeviction`, 64 MiB `maxmemory` and the 256 MiB container ceiling; never enable TTL-based cache eviction here.
- Existing RDB-only data requires approved live conversion with stopped writers (`scripts/compose.sh redis-adopt`) before recreation. Deployment/manual convergence fails closed until AOF is ready. Never disable AOF or remove its files to bypass the guard.
- Infra rollback retains the adopted Redis command. After rollback to pre-A07 Git config, restore compatible Redis config/helpers before further operations. The temporary override protects the current rollback only.
- Backend fail-open and URL/DB precedence are separate handoffs. Owner acceptance of the loss/error contract is required; fixture baseline tests do not fix application security. See [Redis policy and adoption](https://github.com/vstore-ex/Docs/blob/main/infra/redis-state.md).

### 🔄 6. Migration Ownership
- Alembic migrations run in the app repo's deploy job (`deploy-service.yml`, `run-migrations: true`) behind a `pg_dump` snapshot. Worker sets `AUTO_MIGRATE=false`. Do not add `command: alembic upgrade ...` to compose.
- Every model change requires a migration in the same PR — prod has already been bitten by model columns with no migration (schema drift, found via `alembic check`).

### 🔒 7. Offline Host
- Deploy jobs **fail** when the production host is unreachable — no skip-to-green. The VM is expected to be off at times; on boot containers return via `restart: unless-stopped`. Missed main-pushes are re-applied via Actions → Run workflow (re-run reuses the originally resolved reusable workflow version — prefer a fresh dispatch after callee changes).

## 4. Runbook

```bash
docker compose config --quiet   # validate before any PR
bash scripts/compose.sh up -d --wait  # start with verified release state
bash scripts/compose.sh ps            # healthcheck state
bash scripts/compose.sh logs -f backend  # logs without holding the deploy lock
bash scripts/compose.sh down          # stop (add -v to wipe data — DESTRUCTIVE)
```

All production images come from GHCR. Infra CI builds the edge from `config/nginx/Dockerfile`, refreshes the base/runtime packages, and gates fixable HIGH/CRITICAL vulnerabilities before publishing that exact image. Deploy and rollback use immutable digests without a host rebuild. See [edge security gate and adoption](https://github.com/vstore-ex/Docs/blob/main/infra/edge-security.md).

## 5. Deployment Model

- App repos: `ci.yml` calls `vstore-infra/.github/workflows/build-and-push.yml@main` then `deploy-service.yml@main` (`secrets: inherit`). Digest-pinned, flock-serialized, health-gated, auto-rollback to previous digest.
- This repo: `ci.yml` validates Compose and scans the edge, then publishes and deploys its immutable digest with config in **`~/vstore`** on the host (override with `DEPLOY_PATH` secret). The clone must remain clean. Preserve application pins; publish edge state only after HTTPS API/image verification. Failure restores config and the previous edge artifact without rebuilding.
- Host preconditions: Docker Compose **v2 plugin**, `docker login ghcr.io` (PAT with `read:packages`), `~/vstore/.env` with all fail-fast variables (`DATABASE_URL` must use the service-name host `@postgres:5432/...` and an existing database name; `POSTGRES_DB` only applies at first-volume init), storage dir owned by UID `10001`.
- Release tooling also requires Python 3. Adopt verified running images before rollout; infrastructure CD bootstraps a missing release state before convergence and fails if adoption cannot verify every application image. Use the manual wrapper to share `.deploy.lock`; plain Compose reads the persisted override but does not lock.
- Requires org setting: *Actions → Allow reusable workflows from private repositories* — single dependency of the deduplicated CI; without it every cross-repo call fails at startup.

Deployment recovery reloads dependent Nginx proxies and checks both HTTPS API routes before publishing release state. Use `bash scripts/compose.sh recover` after out-of-band recreation. See [upstream recovery](https://github.com/vstore-ex/Docs/blob/main/infra/upstream-recovery.md).

Deployment paths belong to the SSH user: clone/backups `0700`, secrets and snapshots `0600`. Both SSH workflows use `umask 077`; `scripts/secure-deployment.py --lock-only` protects the deployment directory and refuses unsafe lock paths before opening the lock, then the full helper runs under the deployment lock. Never traverse snapshots or release state before acquiring it: another deploy can rename or prune those files. Preserve app storage ownership/modes; never recursively chmod/chown the full clone. Install the helper before updated app CD. See [deployment permissions](https://github.com/vstore-ex/Docs/blob/main/infra/deployment-permissions.md).

Off-host backups are opt-in host tooling, with the [runbook in Docs](https://github.com/vstore-ex/Docs/blob/main/infra/offhost-backups.md). Weekly due checks use completed remote recovery points and run after boot even when the VM was offline for weeks. Hold the shared deployment lock for capture and service resume; restart existing containers before upload. Never initialize a repository, prune old copies, install a timer, restore production data or enable writer downtime without the reviewed adoption policy. A pending `backups/.offhost-resume.json` blocks deployments and manual release mutations until `scripts/offhost-backup.py resume` succeeds. Restore only into new targets and verify data, revision and required storage before activating writers.
