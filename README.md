# VStore Infrastructure (`vstore-infra`)

Orchestration, reverse proxy routing, and production infrastructure for the **VStore** game store platform.

---

## 1. System Architecture & Topology

All services are orchestrated via **Docker Compose** inside an isolated bridge network (`app_net`). Public traffic enters exclusively through hardened Nginx reverse proxies, while database, cache, and backend application servers remain bound strictly to `127.0.0.1`.

```
                        Public / Host Traffic
                         │                 │
           Storefront    │                 │  Admin Dashboard
           (Port 3000)   ▼                 ▼  (Port 3001)
                   ┌───────────┐     ┌───────────┐
                   │ frontend  │     │   admin   │ (Nginx :8080)
                   └─────┬─────┘     └─────┬─────┘
                         │                 │
                         └───► /api/ ◄─────┘ (Internal DNS: backend:8000)
                         └───► /static/ ◄──┘
                                   │
                                   ▼
                         ┌───────────────────┐
                         │      backend      │ (FastAPI / Uvicorn)
                         └─────┬───────┬─────┘
                               │       │
             DB Queries ┌──────┘       └──────┐ Task Broker / Cache / Auth
                        ▼                     ▼
                 ┌────────────┐        ┌────────────┐
                 │  postgres  │        │   redis    │ (Redis 7.2)
                 │  (PG 17)   │        │  (DB 0-3)  │
                 └────────────┘        └──────▲─────┘
                                              │ Celery Queue
                                       ┌──────┴─────┐
                                       │   worker   │ (Celery + FFmpeg)
                                       └────────────┘
                                              ▲
                                              │ Shared Volume (./storage)
                                       ┌──────┴─────┐
                                       │ ./storage  │ (Local Media Storage)
                                       └────────────┘
```

---

## 2. Services Specification

| Service | Image | Internal Port | Host Binding | Health Check | Resource Limits | Volumes & Storage |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`frontend`** | `notverysmart/vstore-frontend:latest` | `8080` | `0.0.0.0:3000` | `GET /healthz` | `0.5 CPU, 256M RAM` | `./config/nginx-frontend.conf:ro` |
| **`admin`** | `notverysmart/vstore-admin:latest` | `8080` | `0.0.0.0:3001` | `GET /healthz` | `0.5 CPU, 256M RAM` | `./config/nginx-admin.conf:ro` |
| **`backend`** | `notverysmart/vstore-backend:latest` | `8000` | `127.0.0.1:8080` | `GET /health` | `1.5 CPU, 1024M RAM` | `./storage:/app/storage` |
| **`worker`** | `notverysmart/vstore-backend:latest` | — | — | Process Liveness | `1.0 CPU, 1024M RAM` | `./storage:/app/storage` |
| **`postgres`** | `postgres:17` | `5432` | `127.0.0.1:5432` | `pg_isready` | `1.0 CPU, 1024M RAM` | `postgres_data:/var/lib/postgresql/data` |
| **`redis`** | `redis:7.2-alpine` | `6379` | `127.0.0.1:6379` | `redis-cli ping` | `0.5 CPU, 256M RAM` | `redis_data:/data` |

### Redis Database Allocation
* **DB 0**: Celery Task Broker & Async Result Backend
* **DB 1**: API Response Caching layer
* **DB 2**: IP & User Rate Limiting counters
* **DB 3**: Revoked JWT Token Blacklist

---

## 3. Quick Start & Deployment

### Prerequisites
* **Docker Engine** 24.0+
* **Docker Compose v2** (`docker compose` plugin)

### 1. Environment Setup
Copy the production template and fill in sensitive variables:
```bash
cp .env.example .env
```

Ensure the following variables are configured in `.env`:
* `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
* `DATABASE_URL` (format: `postgresql://<user>:<password>@postgres:5432/<db>`)
* `SECRET_KEY` (generate with `openssl rand -hex 32`)
* `ADMIN_EMAIL`, `ADMIN_PASSWORD`
* `CELERY_BROKER_URL` & `CELERY_RESULT_BACKEND` (default: `redis://redis:6379/0`)
* `AZURE_STORAGE_CONNECTION_STRING` (leave empty or set to `local` for local file storage)

### 2. Prepare Storage Directory (Debian / Linux Production)
Since backend containers run under non-root system user `appuser` (UID `10001`), ensure proper permissions on host storage:
```bash
mkdir -p storage/media
chmod -R 775 storage
```

### 3. Start the Platform
```bash
# Pull latest container images
docker compose pull

# Launch services in background
docker compose up -d
```

### 4. Verify Health Status
Check that all services are `healthy` and running:
```bash
docker compose ps
```

---

## 4. Reverse Proxy & Routing Rules

Nginx configs located in [`config/`](config/) provide reverse proxy routing and caching:

* **`/api/`**: Proxies requests to `http://backend:8000/api/` with client headers (`Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`).
* **`^~ /static/`**: High-priority routing for locally uploaded game assets and thumbnails to `http://backend:8000/static/`.
* **`/healthz`**: Lightweight liveness probe returning `200 OK`.
* **`/assets/`**: Content-hashed build files from Vite cached with `Cache-Control: public, max-age=31536000, immutable`.
* **`/` (SPA Fallback)**: Single-page application routing (`try_files $uri $uri/ /index.html`) with `no-store, no-cache`.

### Content Security Policy (CSP)
Nginx enforces strict Content Security Policies:
* `media-src 'self' blob: https://*.blob.core.windows.net;` ensures video streaming and trailers work from both local storage and Azure.
* `img-src 'self' data: blob: https://placehold.co https://*.blob.core.windows.net;` allows media artwork and avatars.

---

## 5. Security & Infrastructure Hardening

1. **Network Hardening**:
   * PostgreSQL (`5432`), Redis (`6379`), and FastAPI (`8080`) bind exclusively to `127.0.0.1`.
   * Public exposure (`0.0.0.0`) is restricted to Nginx storefront (`3000`) and admin (`3001`).
2. **Log Rotation**:
   * Global `x-logging` anchor enforces Docker `json-file` rotation: `max-size: "10m"`, `max-file: "3"`.
3. **Resource Quotas**:
   * All containers have CPU and memory boundaries (`deploy.resources.limits`) to prevent runaway resource consumption.
4. **Data Persistence**:
   * PostgreSQL and Redis data persist across restarts via named volumes (`postgres_data`, `redis_data`).

---

## 6. Operations & Maintenance Runbook

### Service Inspection
```bash
# Stream aggregated logs
docker compose logs -f

# Follow Celery worker task execution
docker compose logs -f worker

# Follow backend application logs
docker compose logs -f backend
```

### Stopping and Restarting
```bash
# Graceful stop
docker compose down

# Stop and remove volumes (DESTRUCTIVE: resets database & redis)
docker compose down -v

# Restart a single service
docker compose restart worker
```

### Configuration Validation
Before opening PRs or deploying changes, run validation checks:
```bash
# Validate Compose configuration syntax
docker compose config --quiet

# Test Frontend Nginx syntax
docker run --rm \
  --add-host backend:127.0.0.1 \
  -v $(pwd)/config/nginx-frontend.conf:/etc/nginx/conf.d/default.conf:ro \
  nginx:alpine nginx -t

# Test Admin Nginx syntax
docker run --rm \
  --add-host backend:127.0.0.1 \
  -v $(pwd)/config/nginx-admin.conf:/etc/nginx/conf.d/default.conf:ro \
  nginx:alpine nginx -t
```

---

## 7. CI/CD Integration

Automated checks run on GitHub Actions on every pull request and push to `main` ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)):
1. **Compose Validation**: Verifies `docker-compose.yml` against stubs from `.env.example`.
2. **Nginx Configuration Validation**: Validates syntax and upstream resolution for both `nginx-frontend.conf` and `nginx-admin.conf`.
