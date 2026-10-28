# VStore observability — resource and Blob history slice

Implements infrastructure #73 under #63. Backend instrumentation is supplied by
vstore PR #58 (test source revision `0d7ba896728a77c13f06cb8cc2c1d3fcd0cdab61`).
That PR must be merged and its image verified before adoption. Existing health
and Celery endpoints are reused; this change does not replace them.

Owner decision, 2026-10-28: every monitoring process runs on the production VM.
ThinkPad is a UI client only. Logs and queryable metrics history use separate
private Azure Blob containers, separate also from media/backups. This supersedes
the original filesystem-only five-service decision. No resources or production
recipients are created/activated by this PR.

```text
VM/container exporters + /health + authenticated backend /metrics
  Alloy → Prometheus (fresh TSDB, rule evaluation in later #75)
             Thanos sidecar → metrics Blob ← Store Gateway
                              ↓                ↓
                         singleton Compactor   Query → Grafana
Docker logs → Alloy → Loki (WAL/cache + TSDB index/chunks in logs Blob) → Grafana
```

The four Thanos processes are necessary for upload, historical queries and
retention. They add compute overhead; object storage offloads retained disk,
not CPU/RAM. Alertmanager/rules, DB/worker/deployment/backup signals and final
resource-fit acceptance remain #74–76. No whole-host outage detection is provided
by a colocated evaluator. When the VM is off, collection, UI and alerts are off.

## Private configuration and explicit startup

Production adoption is a separate authorized operation. Do not run these steps
on production merely because configuration/CI passes.

1. Provision/approve two **existing private** containers through the owner's
   Azure process, with separately scoped credentials. `prepare` never provisions;
   Thanos container creation is explicitly disabled. Connection strings must
   include `AccountName`; SAS strings may include it alongside `BlobEndpoint`.
   Limit credentials to the intended container's read/list/write/delete access.
2. Supply the six values in `.env.example` using the host secret mechanism.
   `METRICS_TOKEN` must exactly match the verified backend's environment variable.
   Root Compose forwards it to the backend with an empty/disabled default.
   Do not put credentials in command arguments, shell history, URLs, or Git.
3. Prepare a **new absolute directory**, outside the checkout, under a private
   parent. This writes only files, never starts services:

   ```bash
   python3 scripts/monitoring.py prepare --directory /absolute/private/vstore-monitoring
   ```

   Directory mode is 0700; every file is 0600. Generated `compose.env` selects
   the preparing UID/GID so nonroot containers can read file-backed secrets on
   Linux. Initialization changes ownership only on this project's new named-volume
   roots; Alloy's state directory belongs to root. It never chmods/chowns app data.
   Existing configuration is refused, making rotation an explicit new directory.
4. Append `VSTORE_COMPOSE_PROJECT` to that private
   `compose.env` if your app project differs from `vstore`. Set `MONITORING_API_ADDRESS` if the loopback backend binding differs.
   Host collector URLs and port overrides are listed in `.env.example`.
5. After approval, use a unique monitoring project and the private env file:

   ```bash
   docker compose -p vstore-monitoring --env-file /absolute/private/vstore-monitoring/compose.env -f monitoring/compose.yml config --quiet
   docker compose -p vstore-monitoring --env-file /absolute/private/vstore-monitoring/compose.env -f monitoring/compose.yml up -d
   ```

   No application deployment or standard `scripts/compose.sh up` starts this
   standalone project. Grafana is published on `127.0.0.1:3300`. Prometheus remote write and Loki ingestion
   are bound only to `127.0.0.1:19090` and `127.0.0.1:13100` for the host-network collector. Use an
   approved SSH tunnel (`ssh -L 3300:127.0.0.1:3300 <production-alias>`) for access.
   Query, Store and Compactor have no published ports. Alloy listens only on
   `127.0.0.1:12345` in the host network namespace.
   Blob needs outbound network access; the monitoring network is not isolated
   from the internet. Alloy uses host networking so network counters refer to VM NICs, not container
   interfaces. It scrapes the existing loopback backend binding at `127.0.0.1:8080`.

Grafana's provisioned overview shows CPU, memory, filesystem/network,
container resources, API health, request/5xx rates, p95 header latency, scrape
freshness and logs. Empty series display **No fresh data**, never an invented 0.
Use Explore with `{service="backend"} | json | request_id="RETURNED-ID"`.
IDs remain parsed fields, not stream labels. Thanos Query is the metrics datasource,
so recent and Blob history share one interface. Grafana metadata persists in its
local volume (SQLite, single instance); dashboards/datasources are provisioned.

## Collector exception for review

Alloy is the **only privileged collector exception**. cAdvisor requires Linux
host cgroups/devices and host mounts. Alloy uses privileged mode, host PID/cgroup
namespaces, read-only root/proc/sys/Docker storage mounts and Docker/containerd socket access.
The containerd namespace is `moby`; set `MONITORING_CONTAINERD_SOCKET` to the actual
existing runtime socket (commonly `/run/containerd/containerd.sock` or
`/run/docker/containerd/containerd.sock`). The bind fails closed if it is absent.
**Privileged mode supersedes capability dropping**; `no-new-privileges` does not
remove that power. A read-only socket mount does **not** make Docker's API read-only:
this is root-equivalent host access. The other long-running processes run as the
preparing user with capabilities dropped and `no-new-privileges`. The one-shot
volume initializer has only CHOWN and runs no user-supplied scripts.

The exception is part of this PR's review, not permission to activate it on
production. Images are pinned by immutable digest, public listeners are absent; host collector listeners are loopback-only,
configuration is read-only, and Docker log discovery is restricted to the app
project. The collector has no arbitrary remote-config source configured. Do not
attach untrusted workloads to the monitoring network. Docker Desktop cannot
prove Linux host/per-container coverage; use the Linux fixture for that evidence.

## Retention, local disk and failure behavior

- Prometheus keeps a **6h target window**, 512 MB block size ceiling and equal
  2h min/max block durations. WAL/head data need additional space; the size ceiling
  excludes them. Sidecar uploads completed blocks, normally every 2h. The latest
  unshipped interval is not in Blob; local persistent state is still required.
- Seven-day raw metric retention is enforced by the singleton Compactor, with
  downsampling disabled. Retention deletion can lag while the VM/compactor is off;
  query results older than retention may persist until compaction/deletion runs.
- Loki uses seven-day retention/reject-old limits, TSDB on Azure, local WAL,
  active index, a 1h index cache, and bounded ingestion/query concurrency. Its
  compactor deletes asynchronously (2h delay). Retention is not a wall-clock Blob
  lifecycle promise while the VM is off.
- Alloy remote-write WAL has a 30m maximum keepalive; lag beyond it can lose metric
  samples. Docker source positions persist. App Docker logs rotate at 10 MB × 3
  per container. Loki writes retry eight times with backoff capped at 30s; beyond
  that window, batches may be dropped. This is bounded retry, not lossless delivery.
- CPU/memory ceilings and caches are **initial test ceilings**, not a demonstrated
  production fit. Named volumes do not enforce filesystem quotas. WAL, compaction
  scratch and active indexes can grow during failures. Before activation, #76
  must establish a dedicated disk/quota/reserve budget and exhaustion behavior,
  measure representative application impact and adjust ceilings. Never claim
  the 512 MB TSDB flag bounds total monitoring disk or silently upgrade the VM.
- Rules in #75 must fit inside the local window. Increase retention and remeasure
  if an agreed rule needs a longer lookback; a Blob query adapter is not a source
  for Prometheus's local rule engine by itself.

## Diagnosis and safe stop

Use the same `-p`, `--env-file` and `-f` arguments as startup for every command.
Check `ps`/`logs --tail 100` for stopped/OOM containers. If the UI is unavailable,
first verify VM availability and the tunnel; there is no off-host observer.
If API panels are empty, check Alloy component health, backend `/health`, matching
collector token, actual network and backend instrumentation image. `up=0` is a
failed scrape; absent/stale data is unknown. If older ranges fail, check Store,
sidecar upload errors, Blob connectivity/permissions and compactor failures;
recent local data does not prove historical storage works. Existing cached
queries may still succeed during a Blob outage.

Use `docker stats --no-stream` plus host disk/quota tools to diagnose pressure.
Logs may contain exception messages/application data: restrict access and avoid
secrets in application exceptions. Do not dump rendered private configuration in
CI or support tickets. Never delete WAL/AOF, prune backups or recreate app services
as a monitoring repair.

Stop with `docker compose ... down` **without `-v`**. Only this monitoring project
stops; app data/services/application networks remain intact. Named-volume history and
Blob objects survive. Restart with the same project identity and private env file.

## Reproduce local evidence

No Azure account is needed. Export the instrumentation revision without private
files, install the small fixture dependency in an isolated environment, and run:

```bash
mkdir /tmp/vstore-monitoring-backend
# Run in the backend repo; use the merged revision once available.
git archive 0d7ba896728a77c13f06cb8cc2c1d3fcd0cdab61 | tar -x -C /tmp/vstore-monitoring-backend
# Run in vstore-infra:
python3 -m venv /tmp/vstore-monitoring-test-venv
/tmp/vstore-monitoring-test-venv/bin/pip install -r tests/fixtures/monitoring/requirements.txt
/tmp/vstore-monitoring-test-venv/bin/python tests/run_monitoring.py --backend-context /tmp/vstore-monitoring-backend --report /tmp/monitoring-evidence.json
```

The runner uses a random `vstore-obs-*` project, a real backend image from the
exported source and local Azurite with public dummy credentials. It creates only
local emulator containers, verifies official validators, metrics, Grafana, 500/log
correlation, an 8h block uploaded by real sidecar and queried after source shutdown
and Store/Query restart, logs queried with **empty** Loki WAL/index/cache, and Blob
outage/recovery. It removes only its fixture containers/volumes in `finally`.
Linux additionally requires named-container metrics. macOS uses a narrower mount
override and explicitly reports the host/cgroup limitation. Synthetic fixture
load and emulation are not production adoption, full seven-day wall-clock
retention evidence, outage-buffer exhaustion evidence, or proof of #76 fit.

CI runs `tests/test_monitoring.py` and official Prometheus/Alloy/Loki validators
without network access, host mounts or collector privileges. The full real-backend
fixture is explicit because the backend is a separate private repository and this
PR does not introduce a cross-repository credential into CI.

Primary references:
[Loki Azure/TSDB storage](https://grafana.com/docs/loki/latest/configure/storage/),
[Thanos sidecar](https://thanos.io/tip/components/sidecar.md/),
[Thanos Azure config](https://github.com/thanos-io/thanos/blob/main/docs/storage.md#azure),
[Alloy cAdvisor privileges](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.cadvisor/).

## Authorized Azure check and current adoption gate

On 2026-10-28 the owner explicitly authorized creating private
`vstore-obs-logs` and `vstore-obs-metrics` in the existing `vstorestorage` account.
The in-place application SDK verified upload/download hash equality, anonymous
404, and deletion of the synthetic test blobs. `media` was untouched; no storage
account policy or production monitoring process was changed. See
[evidence/2026-10-28-azure.json](evidence/2026-10-28-azure.json). This verifies real
Azure connectivity/privacy, not Loki/Thanos production adoption.

A read-only VM measurement found 873904 KiB total RAM (853 MiB), with 212204 KiB
available at the check (207 MiB). The complete current stack is **not approved
for production adoption** on that envelope. #76 must establish a measured fit or
return a resource/architecture decision to the owner. This draft does not resize
the VM or activate monitoring.

The isolated Linux snapshot used approximately **711 MiB** across the eight
monitoring processes, excluding the test backend and Azurite. This is synthetic
load, not a peak bound; it already exceeds the VM's measured available memory.
See [evidence/2026-10-28-linux.json](evidence/2026-10-28-linux.json).
