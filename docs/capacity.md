# Capacity experiment (A15)

This laboratory measures a bounded catalog/media workload and the additional
memory used by a deployment. It does not run against production. The outcome is
a capacity recommendation for owner review, not an automatic VM resize or a
claim that every possible upload fits.

## Run the laboratory

Requirements: Docker Engine with Linux cgroup v2, Python 3.10+, locally available
application/edge images, `postgres:17`, `redis:7.2-alpine`,
`certbot/certbot:v3.1.0`, and
`mcr.microsoft.com/azure-storage/azurite:3.35.0`. Build application images from
reviewed source revisions using their existing Dockerfiles. The backend requires
FFmpeg and its normal runtime Python dependencies. Do not supply production
credentials. The runner resolves local application tags to image IDs before
starting the experiment and writes their identity/architecture/revision to the
manifest. Auxiliary image identities are also retained in the container records.

```bash
python3 scripts/capacity-lab.py \
  --backend-image local-backend:capacity \
  --frontend-image local-frontend:capacity \
  --admin-image local-admin:capacity \
  --proxy-image local-edge:capacity \
  --output .capacity-results/baseline --seconds 60

python3 scripts/capacity-lab.py \
  --backend-image local-backend:capacity \
  --frontend-image local-frontend:capacity \
  --admin-image local-admin:capacity \
  --proxy-image local-edge:capacity \
  --profile config/capacity/candidate.json \
  --output .capacity-results/candidate --seconds 60
```

Run profiles sequentially, on the same host with the same images and workload.
Each output directory must be empty. `--seconds` permits 30–600 seconds;
`--clients` permits 1–16 readers and `--uploaders` 1–4 uploaders. Defaults are four
readers, two uploaders and a 200 ms delay per reader. These are closed-loop
clients; achieved throughput depends on response time and is reported rather
than advertised as a fixed request rate. For a repeat, use a new output directory.

The baseline sets Celery concurrency to two, matching the two-vCPU production
host observed during the experiment. CPU quotas otherwise come from the base
Compose file. The candidate uses one prefork process and explicit test limits.
`config/capacity/candidate.json` affects only the laboratory. Production Compose,
deployment settings and Redis durability policy are not changed.

## Isolation and cleanup

The runner reads only the explicitly selected base Compose file with a private,
generated environment file. It discards Compose's production volume/network
names, external state, host port bindings, storage bind mounts and application
environment. Every run gets a random `capacity-*` project with new PostgreSQL,
Redis, uploads, storage, certificate and Azurite volumes. Both application
networks are internal. No arbitrary endpoint option exists. Google/cloud
credentials are absent; Blob Storage is the project-local Azurite account.
Certbot's timer is represented by an idle shell, with no ACME requests.

App hardening is preserved. A short, networkless initializer has only `CHOWN` to
assign the two newly created application volumes to UID 10001. The collector has
no Docker socket, no network and no Linux capabilities; host PID/cgroup
namespaces plus a read-only cgroup mount allow it to inspect only the PIDs in the
experiment's container list. This is intended for a trusted test Docker host.

Normal and failed runs retain measurements/exit states and remove only their
own project containers, volumes and networks in `finally`. An OS kill or Docker
daemon failure can bypass that cleanup: inspect the exact project in
`manifest.json`, then remove it; never use a global prune or another project's
volumes. Temporary environment/config files contain generated test credentials
and are removed. Reports omit credentials and SQL dumps. `.capacity-results/`
and `.capacity-cache/` are ignored by Git.

## Workload and phases

Fixtures are synthetic: 100 catalog games, one deterministic 3840×2160 RGB PNG
(seed 42), a five-second 1920×1080/24 fps H.264 video, and 16 MiB of Redis payload.
Actual fixture sizes/hashes and Python dependency versions are retained. There
is no copy of production user data.

1. **Idle:** ten seconds after fixture preparation.
2. **Mixed:** catalog list/detail reads through edge HTTPS and storefront Nginx;
   authenticated image/video uploads through admin Nginx. Images use the actual
   Pillow/WebP implementation and Azure SDK. Videos use the actual Celery queue,
   FFmpeg thumbnail path, Blob upload and database update. A 90-second drain
   verifies every accepted video has a completed URL and thumbnail.
3. **Deploy:** repeat the mixed workload; after ten seconds, stream `pg_dump`
   through gzip and run `alembic upgrade head` in a separate container while the
   current backend/worker remain live. The schema is already current, so this
   measures migration startup/overlap, not an arbitrary expensive data migration.
   Trigger Redis `BGSAVE` while writing 64 KiB values at ten writes/second.
4. **Cooldown:** ten seconds after tasks drain.
5. **Recreate:** recreate backend/worker and wait for backend health after traffic
   drains. Sampling follows replacement container IDs. This measures restart
   memory, not zero-downtime request availability.

Tests assert project/state isolation, static IP separation for the migration
container, simultaneous peak accounting and refusal of incomplete measurements.
The expensive workload is opt-in, while these guards run in normal Infra CI.

## Metrics and interpretation

`samples.jsonl` records Linux host MemAvailable, swap occupancy and memory PSI;
each container's cgroup `memory.current`, anonymous memory, swap,
`memory.events`, `memory.peak`, PSI and process RSS sum at roughly 500 ms intervals.
The cadence may be slower while reading many processes. Missing cgroup/PSI
support fails the experiment rather than generating fake zero values.

Working set is `memory.current - inactive_file`, floored at zero. It is an
approximation, not a guaranteed unreclaimable footprint. For VM sizing use the
largest **simultaneous** sum of application/database/proxy containers plus the
transient migration. Do not sum per-service maxima from different timestamps,
and do not size the VM from the sum of configured ceilings. Azurite, client and
collector overhead are excluded from the application sum and still affect the
host's available memory/PSI. Auxiliary peaks are reported separately.

RSS sums include shared prefork/PostgreSQL pages more than once; keep them as
diagnostics rather than adding them to the VM budget. Cgroup kernel peaks include
page cache/startup and help validate per-container ceilings. A half-second
sample can miss a brief simultaneous peak; kernel peaks are per-container, not
a substitute for a simultaneous stack measurement. PSI deltas are microseconds
of stall time; occupied swap is not by itself evidence of ongoing swapping.

Reports include `manifest.json`, `fixtures.json`, `containers.json`, phase load
results, raw samples and `summary.json`. Success requires all phases and actual
video completion, with no unexpected restart/OOM. Proposed profiles also require
at least 15% margin below each application's cgroup kernel peak and no limit
enforcement events. Baselines expose these margins without enforcing them.
Request counts and p95 latency
are split by catalog/image/video; a successful HTTP upload alone is insufficient.

Sources: [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html),
[Linux PSI](https://docs.kernel.org/accounting/psi.html),
[Celery worker concurrency](https://docs.celeryq.dev/en/stable/userguide/workers.html#concurrency).

## Production rollout remains separate

Before adopting a profile, replay on an isolated Linux amd64 host with the exact
reviewed production images and representative media/data sizes. Account for host
OS/Docker, PostgreSQL growth, Redis persistence and backup overlap, and sustained
CPU limits. Desktop ARM timings cannot certify production throughput or a VM
size. This draft delivers the experiment tooling and observed results; the final
768 MiB worker profile has not completed a validated run. A15 remains open.

During the first overlap attempt, `compose run backend` failed with **Address
already in use** because the base service's fixed proxy IP was held by the live
backend. The lab now uses a migration-only copy on `app_net`; no business code or
schema changes are required. The production reusable deployment workflow still
runs its migration as `backend`; investigate/fix that network collision in a
separate deployment change before the next backend release. See the dated report
for the exact reproduction and the sizing evidence.
