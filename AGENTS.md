# VStore Infrastructure (`vstore-infra`) - Agent Guidelines & Runbook

Conventions, constraints, and verification procedures for agents and DevOps engineers working in **`vstore-infra`**.

---

## 1. Repository Scope

`vstore-infra` owns:
- **Docker Compose** orchestration (`docker-compose.yml`) — the full production stack.
- **Shared CI/CD**: reusable workflows (`build-and-push.yml`, `deploy-service.yml`) that every app repo calls for image builds and SSH deploys.
- **Infra CI** (`ci.yml`): compose validation + config sync to the production host.

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

### 🔄 5. Rollback Preflight & Image Pinning
- App deploys load verified digests from the host-local `docker-compose.override.yml` under the shared flock, then test candidates with `release_compose` (base file plus exported `*_IMAGE`). Never rely on `:latest` for a deploy or a rollback. Publish current and previous digests atomically only after health and image verification; see `docs/releases.md`.
- The deploy preflight refuses to run if the current container's image has no `RepoDigests` (no valid rollback target). `RepoDigests` lives on the **image** — it must be read via two-step inspect (container `.Image` → image). Do not "simplify" this back to a single inspect.
- Rollback restores the container image only. DB schema is never downgraded automatically; the deploy job prints the snapshot-restore and `alembic downgrade` paths.

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

Only the edge proxy builds locally from `config/nginx/Dockerfile` to provide the OpenSSL CLI. Application images come from GHCR. Build the proxy with `docker compose build proxy` before applying TLS configuration.

## 5. Deployment Model

- App repos: `ci.yml` calls `vstore-infra/.github/workflows/build-and-push.yml@main` then `deploy-service.yml@main` (`secrets: inherit`). Digest-pinned, flock-serialized, health-gated, auto-rollback to previous digest.
- This repo: `ci.yml` validates compose, then SSH-syncs config (`git reset --hard origin/main` + `docker compose up -d --wait`) in **`~/vstore`** on the host (override with `DEPLOY_PATH` secret) — that directory must remain a clean git clone of this repo.
- Host preconditions: Docker Compose **v2 plugin**, `docker login ghcr.io` (PAT with `read:packages`), `~/vstore/.env` with all fail-fast variables (`DATABASE_URL` must use the service-name host `@postgres:5432/...` and an existing database name; `POSTGRES_DB` only applies at first-volume init), storage dir owned by UID `10001`.
- Release tooling also requires Python 3. Adopt verified running images before rollout; infrastructure CD bootstraps a missing release state before convergence and fails if adoption cannot verify every application image. Use the manual wrapper to share `.deploy.lock`; plain Compose reads the persisted override but does not lock.
- Requires org setting: *Actions → Allow reusable workflows from private repositories* — single dependency of the deduplicated CI; without it every cross-repo call fails at startup.
