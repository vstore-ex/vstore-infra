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

   Run it as the unprivileged account that owns monitoring, never with `sudo`:
   `prepare` refuses UID/GID 0 because the generated runtime identity would
   otherwise run every nonroot service as root.
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
- **Blob outage tolerance is about 5h.** Prometheus deletes local blocks once
  they fall outside the 6h window (measured from its newest block), whether or
  not the sidecar has uploaded them. A block is cut about an hour after its 2h
  range ends, so an outage longer than roughly 5h permanently loses unshipped
  blocks; nothing re-uploads them. This is derived from block timing, not
  measured; an exhaustion drill remains part of #76.
  Prometheus scrapes the sidecar and Compactor, and the dashboard shows the age
  of the last successful upload and failed uploads in the last hour. Alerting
  on these signals belongs to #75. Increase `retention.time` (and the disk
  budget) if a longer outage must be survivable.
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
The resulting cgroup must be `/vstore.slice/vstore-monitoring.slice`. The
guard reads the expected limits from
[`systemd/vstore-monitoring.slice`](systemd/vstore-monitoring.slice), the single
reviewed source; print them with `bash scripts/monitoring-guard.sh budget`.

**Migrating the 2026-10-30 slice.** Production still has the earlier 256 MiB
RAM + 512 MiB swap unit. `check` now reports a limit mismatch and `install`
refuses to overwrite it. After separate authorization, with monitoring stopped,
confirm `/sys/fs/cgroup/vstore.slice/vstore-monitoring.slice/cgroup.events`
reports `populated 0`, then run
`sudo systemctl disable --now vstore-monitoring.slice`, remove only
`/etc/systemd/system/vstore-monitoring.slice`, `sudo systemctl daemon-reload`,
and the `install` and `check` commands above.

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

The aggregate budget is **1536 MiB RAM (reclaim from 1280 MiB) and no swap**;
CPU is capped at 50% of one core and tasks at 256. Grafana has its own 640 MiB
ceiling. Earlier budgets failed on production:

- 256 MiB + 512 MiB swap (2026-11-02): the stack swapped about 224 MiB on the
  shared disk and application health checks timed out.
- 768 MiB, no swap (2026-11-07): it passed a 30-minute window, but about 20
  minutes later the processes held 629 MiB, leaving 25 MiB of page cache. With
  no swap the kernel did not OOM-kill anything; it kept evicting and re-reading
  program and data files instead. Host-wide I/O pressure reached about 90%
  (load 9.3) and the stack stopped scraping. Grafana sat at 370 MiB of its
  384 MiB cap. Stopping monitoring restored the host within a minute.

A memory ceiling without swap does not end in a clean OOM kill: below the
ceiling the kernel prefers to thrash file cache, and that disk traffic is shared
with the application. The budget therefore leaves at least 512 MiB of cache room
above the observed sustained use, and `MemoryHigh` starts reclaim inside the
slice before the hard ceiling. The VM has 3,872 MiB; the application used about
1,180 MiB, so at least 1 GiB stays free outside monitoring. systemd-oomd is not
installed on production; pressure-based killing would be a separate host change.
Change the budget only in the slice unit, with a new measurement, and stop
monitoring on sustained I/O pressure (see below).
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

### Production trial at the 768 MiB budget, 2026-11-07 (superseded)

The owner-authorized trial migrated the production slice with the guard and ran
the full stack for 30 minutes on the resized VM. Application probes saw 0 errors
in 1,800 samples; `/health` p95 stayed at about 6 ms and the games API p95 moved
from 8.7 to 9.8 ms (p99 14.6 to 34.0 ms). The slice held at its ceiling with
552 MiB anonymous memory and the rest reclaimable cache, with no swap and no OOM
kills, and the sidecar uploaded its first block to Blob. Backend metrics were
not scraped because the running backend had no token. See
[the trial evidence](evidence/2026-11-07-production-768-trial.md).

That window was too short: the stack thrashed about 20 minutes after it ended
and was stopped at 22:41 UTC (see the budget section above and the evidence
addendum). The 768 MiB budget was replaced.

**Stop condition for every production run:** stop the monitoring project when
`/proc/pressure/io` reports `some avg300` above 20% (the kernel's five-minute
average), or when the slice records a new OOM kill. The boot-managed watchdog
below applies this rule every minute. Also stop on a rising `high` counter in
the slice's `memory.events` together with application latency above the
baseline. Trials must last at least three hours so that slow growth (Grafana,
Loki caches) shows up.

## Boot-managed service (#80)

Three units in `systemd/` run monitoring without a login after every boot:

- `vstore-monitoring.service` (oneshot, stays active): after `docker.service`
  and the slice, `scripts/monitoring-service.sh start` waits up to 300 s for
  the backend `/health`, runs `monitoring-guard.sh check`, and starts the project
  with `-p vstore-monitoring`, the private `compose.env` and both Compose files
  from the deployment checkout. A failed health wait or guard check leaves
  monitoring stopped. `systemctl stop` runs `docker compose stop`; it never
  removes containers or volumes.
- `vstore-monitoring-watchdog.timer` runs `vstore-monitoring-watchdog.service`
  every minute (first check five minutes after boot). It stops
  `vstore-monitoring.service` when `some avg300` is above 20% or the slice has
  an OOM kill newer than the last start, writes the reason to the journal and
  `/run/vstore-monitoring/last-stop`, and never touches application containers.
  Monitoring then stays down until a person starts it again.
- Containers keep `restart: "no"`; systemd owns the lifecycle.

Install (root, after the slice is installed and the private directory has been
prepared by the unprivileged monitoring owner):

```bash
sudo bash scripts/monitoring-guard.sh install-service --env-file /absolute/private/vstore-monitoring/compose.env
sudo systemctl start vstore-monitoring.service   # explicit first start
sudo bash scripts/monitoring-guard.sh status
```

`install-service` refuses a private env file that is not 0600 in a 0700
directory, owned by root, or not pointing at its own directory. It writes only
non-secret paths to `/etc/vstore-monitoring/service.env` (the deployment checkout
it was run from and the env file), installs the units only when no different
units exist, and enables them for the next boot without starting anything.

The units run as root and execute scripts from the deployment checkout, which
the deployment user can write. That user already has passwordless sudo on the
production host, so this adds no new privilege; restrict both together if that
changes. Diagnose with `journalctl -u vstore-monitoring.service -u
vstore-monitoring-watchdog.service`. To disable: `sudo systemctl disable --now
vstore-monitoring-watchdog.timer vstore-monitoring.service`.

## Alert runbook

Prometheus evaluates `rules/vstore-alerts.yml` every 30 s and sends alerts to
Alertmanager (in the slice, no published port; Grafana datasource "VStore
Alerts"). Thresholds are **initial, tunable values** (#75); #65 has not fixed
final ones. Rule behaviour is tested with `promtool test rules
monitoring/rules-tests/vstore-alerts.test.yml` (kept outside `rules/`, because
Prometheus loads every `*.yml` there).

| Alert | Fires when | First checks |
|---|---|---|
| `VStoreApiDown` (critical) | `/health` probe fails for 2 min | `docker compose ps backend`, backend logs in Grafana (`service=backend`), `curl 127.0.0.1:8080/health` |
| `VStoreApiHealthUnknown` (warning) | no probe result for 5 min | Alloy running? `VStoreScrapeFailing`? Unknown is never treated as healthy |
| `VStoreScrapeFailing` (warning) | any target `up == 0` for 5 min | target named in `job`; for `api`, the metrics token and backend `/metrics` |
| `VStoreHighErrorRate` (critical) | > 5% 5xx at > 0.1 req/s for 10 min | logs filtered by `request_id` of a failing request; recent deployment |
| `VStoreSlowResponses` (warning) | API p95 > 1 s for 10 min | VM CPU/memory/I/O panels, PostgreSQL and Redis containers |
| `VMDiskSpaceLow` (critical) | root filesystem < 10% free for 15 min | `df -h /`, `docker system df`; never delete volumes, WAL or backups to recover |
| `VMMemoryLow` (critical) | MemAvailable < 10% for 10 min | container memory panel; stop monitoring first (`systemctl stop vstore-monitoring.service`) |
| `VMIOPressureHigh` (warning) | processes stalled on I/O > 20% of the time for 10 min | if monitoring is running, the watchdog stops it at the same level; otherwise the application is the cause |
| `MetricsBlobUploadStale` (warning) | no successful block upload for 4 h (the first block ships about 3 h after start) | sidecar logs, Blob credential expiry (SAS until 2026-12-02); history older than ~5 h is lost if this lasts |
| `MetricsBlobUploadFailing` (warning) | any upload failure in the last hour | sidecar logs; Azure Blob availability and permissions |

**Routing.** Alerts are grouped by name (first message after 30 s, updates every
5 min), repeated every 4 h, or every 1 h for critical alerts. While
`VStoreApiDown` fires, it suppresses the API's error-rate, latency and scrape
alerts. Resolved messages are sent (`send_resolved: true`); REPORT-V2 listed
recovery messages as a proposal, so disable them in `scripts/monitoring.py` if
unwanted. `tests/smoke_alert_routing.py` proves grouping and suppression against
a local webhook receiver in CI.

**Delivery.** The repository config has no external recipient: alerts are
visible in Alertmanager and Grafana only. To enable Telegram, set both
`MONITORING_TELEGRAM_BOT_TOKEN` and `MONITORING_TELEGRAM_CHAT_ID` (numeric) when
running `prepare`; it writes a private `alertmanager.yml` (0600) and points
`MONITORING_ALERTMANAGER_CONFIG` at it. Setting only one fails. The message
template is `alertmanager/telegram.tmpl`. Real delivery is a separately
authorized test; do not put the token or chat ID in Git.

**Limits.** Alerting runs on the same VM. When the VM, Docker or its network is
down, nothing is evaluated or sent; a planned shutdown is therefore silent, and
every `for:` window restarts after boot. Detecting a whole-VM outage needs an
external check, which is a separate decision.


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

A read-only VM measurement on 2026-10-28 found 873904 KiB total RAM (853 MiB),
with 212204 KiB available (207 MiB). After that, with explicit owner approval,
the VM was resized from `Standard_B2ats_v2` to `Standard_B2als_v2`; SSH after the
reboot reported 3,965,872 KiB usable guest RAM (about 3.78 GiB). Only the VM size
changed. Re-measure available memory with the application running before any
trial. The resize does not by itself approve production adoption: #76 must still
accept measured fit, disk use and application impact. This PR does not activate
monitoring.

The isolated Linux snapshot used approximately **711 MiB** across the eight
monitoring processes, excluding the test backend and Azurite. This is synthetic
load, not a peak bound; it already exceeds the VM's measured available memory.
See [evidence/2026-10-28-linux.json](evidence/2026-10-28-linux.json).
