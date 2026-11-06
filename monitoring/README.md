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
   include `AccountName`. Thanos accepts a metrics-container SAS alongside
   `BlobEndpoint`. The pinned legacy Loki Azure client does **not** support SAS
   correctly (upstream [#21596](https://github.com/grafana/loki/issues/21596));
   `prepare` rejects a logs SAS before writing files. Loki currently requires a
   shared-key connection string. A storage account key has account-wide access;
   do not describe it as container-scoped. Keep it private on the trusted host.
   Container-scoped identity support remains a separate implementation choice.
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

## Isolated Grafana resource probe (#67)

The probe compares the Grafana image in this stack with a candidate previously
tested in the resource experiment. It uses a new SQLite state directory each
run, identical 128 MiB RAM and 0.5 CPU limits, no swap, and no network or host
mounts. It exercises the authenticated organization API and checks that the
same API rejects anonymous requests. It does not exercise dashboards or data
sources. It never launches the monitoring Compose project.

Run on local ARM64 Docker Desktop with cgroup v2. If the pinned images in
`tests/probe_grafana_footprint.py` are absent, pull them explicitly before the
measurement. Use a new report path for each run; the second command is a
384 MiB control with all other limits unchanged:

```sh
python3 tests/probe_grafana_footprint.py --report /tmp/vstore-grafana-fit.json
python3 tests/probe_grafana_footprint.py --memory-mib 384 --report /tmp/vstore-grafana-control.json
```

Compare the count and first completion time of authenticated HTTP 200
responses within the observation window, container exit/OOM state, and cgroup
peak and memory-limit events. The final authenticated and anonymous requests
are separate post-window controls (expect HTTP 200 and 401 when ready). They do
not count toward the window even if the last in-window check timed out.
HTTP status 0 means the probe did not receive an HTTP response. On SIGINT or
SIGTERM the probe finishes its current bounded Docker command, then cleans up
its container and any anonymous volumes. SIGKILL cannot run cleanup.
Elapsed time starts after `docker run -d` returns; the Docker probe processes
also briefly use resources in the measured cgroup. This is a cold-start
component screen, not a load benchmark, version recommendation, or evidence of
fit on the production VM. Full-stack limits and application impact, restart and
history queries remain part of #67/#76.

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

## Production OOM containment (explicit opt-in)

On 2026-10-30 the reviewed guard was installed and its kernel limits verified
on production. The slice was **empty** (zero RAM/swap usage); no monitoring
containers were active. All eight application container IDs and start timestamps
were unchanged and `/health` returned 200 with the database connected.
The running backend `/metrics` still returned 404. This is host protection
installation only, not full monitoring adoption or resource-fit acceptance.
See [the installation evidence](evidence/2026-10-30-oom-guard.json).

The owner authorized host protection on 2026-10-30. Install the bounded slice
without starting monitoring or touching application containers:

```bash
sudo bash scripts/monitoring-guard.sh install
sudo bash scripts/monitoring-guard.sh check
```

Requires Linux cgroup v2 and Docker's systemd driver. The installer refuses an
existing different unit. It writes only `/etc/systemd/system/vstore-monitoring.slice`,
reloads systemd and enables/starts that empty slice. No daemon restart is needed.
The resulting cgroup must be `/vstore.slice/vstore-monitoring.slice`, with
`memory.max=268435456` and `memory.swap.max=536870912`.

Before **each** authorized production start, run the guard check successfully,
then include **both** files on every Compose command:

```bash
sudo bash scripts/monitoring-guard.sh check &&
docker compose -p vstore-monitoring --env-file /absolute/private/vstore-monitoring/compose.env -f monitoring/compose.yml -f monitoring/production.yml up -d
```

The overlay assigns every monitoring container (including initialization and
privileged Alloy) to the same aggregate parent, sets OOM score adjustment +500,
and disables automatic restart. Monitoring also stays down after a VM or Docker
restart; the owner must explicitly run the guarded startup command above.
After any monitoring OOM, inspect the cause and
budget before explicitly restarting it. Never start production with base Compose
alone: it has only per-container ceilings and remains portable for local fixtures.
For existing monitoring containers, changing parent requires recreation of the
monitoring project only; installation does not move running containers.

The initial **256 MiB RAM + 512 MiB swap** aggregate budget prioritizes the
853 MiB host's application workloads over monitoring completeness. CPU is capped
at 50% of one core and tasks at 256. The previous 711 MiB fixture snapshot does
not prove this RAM/swap budget works: reclaim, peaks and swapping must be measured.
Monitoring may fail entirely under this budget. Do not raise it automatically.
A cgroup-local OOM cannot select an application process outside that subtree;
+500 only biases victim choice during global OOM and does not grant immunity.
The slice reserves no memory for applications, bounds no application growth and
cannot guarantee the whole host never runs out of memory. Privileged Alloy is
trusted root-equivalent software and can alter host policy; this containment is
for resource exhaustion, not a security boundary against a hostile collector.

Production activation remains blocked until backend metrics are verified on the
running image and #76 accepts measured resource/disk impact. Installing an empty
slice is not monitoring adoption. Verify container `HostConfig.CgroupParent`,
`OomScoreAdj`, restart policy and the live parent limits after an eventual start.

Rollback: first stop only the monitoring project with the same two `-f` files
and `down` **without `-v`**. With no remaining processes in its cgroup, run
`sudo systemctl disable --now vstore-monitoring.slice`, remove only the installed
unit if it still exactly matches the reviewed source, then run
`sudo systemctl daemon-reload`. Do not remove a slice containing running containers.
No application image, service, sysctl, swap file or database changes are required.

The local privileged kernel regression is documented in
`tests/smoke_monitoring_oom.py`. It uses a random subtree with an outer 192 MiB
safety ceiling and a 48 MiB monitoring child, observes SIGKILL and `oom_kill`,
and checks an application sibling remains alive. Run it only in a disposable
local Docker VM, never production. It proves kernel cgroup OOM confinement;
it does not replace the production systemd driver/parent verification.

## Diagnosis and safe stop

### Controlled production trial, 2026-11-02

The owner authorized first startup. The running backend already contained the
instrumentation; its token was absent. An external private backend environment
overlay enabled authenticated metrics on the same pinned image, under the shared
deployment lock. Proxy recovery and image verification passed. The matching token
is also in the ignored application `.env`; until PR #77 is adopted, application
recreation must include the private backend overlay to retain metrics.

Every monitoring container was verified under the bounded parent with OOM score
500 and restart disabled. The stack reached its 256 MiB RAM ceiling and used
approximately 224 MiB swap. There was no cgroup OOM kill, but two application
health checks timed out after ten seconds. The trial stopped all monitoring
containers; application health recovered to milliseconds. **The current full
stack failed the availability check within this budget and remains stopped.**
The limits contain memory consumption; they do not eliminate shared-disk swap
contention. Do not repeat the full startup without addressing resource pressure.

A subsequent isolated Loki check used the existing backend account credential
without exporting it. Loki became ready, accepted/query-returned a synthetic log,
and flushed it; Blob objects were verified separately. Thanos had successfully
listed its private metrics container using a scoped SAS, but production block
upload, historical queries and Grafana readiness were not established. The metrics
SAS expires on 2026-12-02 and must be renewed before any later adoption.

See [production trial evidence](evidence/2026-11-02-production-trial.json).
Reduce the footprint and recheck application impact under #76 before claiming
adoption. No VM resize, new Azure resources or real notifications were enabled.

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
