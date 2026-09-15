# VStore Infrastructure (`vstore-infra`) - Agent Guidelines & Runbook

This document defines conventions, operational constraints, architecture details, and verification procedures for AI agents and DevOps engineers working within the **`vstore-infra`** repository.

---

## 1. Repository Scope & Purpose

`vstore-infra` is the central orchestration repository for the VStore platform. It manages:
- **Docker Compose** orchestration (`docker-compose.yml`) for multi-container deployments.
- **Nginx Reverse Proxy** configurations for the storefront (`config/nginx-frontend.conf`) and admin dashboard (`config/nginx-admin.conf`).
- **Networking & Security Policies**: Internal network isolation, port bindings, and Content Security Policies (CSP).
- **Service Reliability**: Healthchecks, startup ordering, resource limits, and log rotation.
- **CI/CD Quality Gates**: Automated validation workflows (`.github/workflows/ci.yml`).

---

## 2. Service Topology & Architecture

All containers run inside the `app_net` bridge network:

```
                  Internet / Host
                   │          │
         Port 3000 │          │ Port 3001
                   ▼          ▼
             ┌──────────┐ ┌──────────┐
             │ frontend │ │  admin   │ (Nginx Unprivileged :8080)
             └────┬─────┘ └────┬─────┘
                  │            │
                  └───┐    ┌───┘
                      ▼    ▼ (Internal Docker DNS: backend:8000)
                  ┌──────────┐
                  │ backend  │ (FastAPI / Uvicorn)
                  └────┬─────┘
                       │ (Internal Docker DNS: postgres:5432)
                       ▼
                  ┌──────────┐
                  │ postgres │ (PostgreSQL 17)
                  └──────────┘
```

### Services Specification

| Service | Image | Internal Port | Host Port | Health Check | Resource Limits |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`postgres`** | `postgres:17` | `5432` | `127.0.0.1:5432` | `pg_isready` | 1.0 CPU, 1024 MB |
| **`backend`** | `ghcr.io/vstore-ex/vstore:latest` | `8000` | `127.0.0.1:8080` | `GET /health` | 1.5 CPU, 1024 MB |
| **`frontend`** | `ghcr.io/vstore-ex/vstore-front:latest` | `8080` | `0.0.0.0:3000` | `GET /healthz` | 0.5 CPU, 256 MB |
| **`admin`** | `ghcr.io/vstore-ex/vstore-admin:latest` | `8080` | `0.0.0.0:3001` | `GET /healthz` | 0.5 CPU, 256 MB |

---

## 3. Strict Rules & Constraints for DevOps Agents

### 🔴 1. Git Workflow: PRs Only
* **Never commit directly to `main`.** Always branch off `main` (`feat/*`, `fix/*`, `chore/*`) and submit changes through Pull Requests.

### 🔒 2. Port Binding & Network Security
* **Never bind internal services to `0.0.0.0`**:
  - `backend` must bind strictly to `127.0.0.1:8080:8000` (or remain unexposed to the host).
  - `postgres` must bind strictly to `127.0.0.1:5432:5432` (or remain unexposed to the host).
  - Only `frontend` (`3000`) and `admin` (`3001`) are exposed on `0.0.0.0`.
* **No Unauthenticated Services**: Never expose caching layers or databases without authentication.

### 🧪 3. CI Nginx Validation Rule
* In isolated CI environments (like GitHub Actions), `nginx -t` tests **must** include `--add-host backend:127.0.0.1`.
  Because `proxy_pass http://backend:8000;` requires upstream hostname resolution at config-parse time, omitting `--add-host` will trigger `[emerg] host not found in upstream "backend"`.

### 🔄 4. Migration Ownership
* Database migrations (`alembic upgrade head`) are exclusively executed inside the backend container's `entrypoint.sh`.
* **Do NOT** add `command: sh -c "alembic upgrade head && ..."` in `docker-compose.yml`, as this causes duplicate migration execution and locking issues.

### 🌐 5. Nginx Proxy Headers
* Reverse proxy blocks (`location /api/`) must always preserve client identity headers:
  ```nginx
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
  ```

---

## 4. Operational Runbook

### Local Stack Management
```bash
# Start all services in the background
docker compose up -d

# View status and healthcheck state of containers
docker compose ps

# Follow aggregated logs
docker compose logs -f

# Follow logs for a specific service
docker compose logs -f backend

# Stop services without deleting database volumes
docker compose down

# Rebuild and restart services
docker compose up -d --build
```

### Configuration Validation
```bash
# Validate Compose syntax and variable interpolation
docker compose config --quiet

# Test Frontend Nginx configuration
docker run --rm \
  --add-host backend:127.0.0.1 \
  -v $(pwd)/config/nginx-frontend.conf:/etc/nginx/conf.d/default.conf:ro \
  nginx:alpine nginx -t

# Test Admin Nginx configuration
docker run --rm \
  --add-host backend:127.0.0.1 \
  -v $(pwd)/config/nginx-admin.conf:/etc/nginx/conf.d/default.conf:ro \
  nginx:alpine nginx -t
```

### Production Deployment (GHCR Private Images)
```bash
# 1. Authenticate Docker with GHCR (one-time on production host, using PAT with read:packages)
echo "$CR_PAT" | docker login ghcr.io -u <GITHUB_USERNAME> --password-stdin

# 2. Pull the latest images from GHCR
docker compose pull

# 3. Apply updates with zero orphan containers
docker compose up -d --remove-orphans
```

