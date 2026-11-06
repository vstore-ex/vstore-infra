# ThinkPad monitoring stack resource screen — 2026-11-06

Issue #67, with evidence for #76. Tested the runtime image pins from PR #77
at `8a9aeb689c56c9628a350a762013883df5759122`, except for the separate
synthetic HTTP fixture and Azurite. The experiment ran on the Arch Linux
ThinkPad (x86-64, Ryzen 7 PRO 4750U, 16 logical CPUs, 13.36 GiB RAM, cgroup v2).

## Method and isolation

A trusted driver created a uniquely labeled Docker project from cached images
without pulls. A fresh synthetic HTTP fixture owned a network namespace with
only loopback; the eight monitoring processes and Azurite joined it. The
fixture generates metrics and handles small CPU-bound PBKDF2 requests. Four
clients send another request 35 ms after completion, validate HTTP 200 and
the response size, and measure latency. The baseline and monitored windows
are each 20 seconds, in that order, with the same app container. No real
VStore, cAdvisor, or host Docker metrics were collected.

Each runtime had 0.5 CPU, no swap, at most 64 tasks, a 32 MiB per-file limit,
a read-only root filesystem and size-limited tmpfs for writable state.
Prometheus and its sidecar shared only a fresh 48 MiB TSDB tmpfs.
Processes ran as UID/GID 10001 with capabilities dropped, no-new-privileges,
no host sockets or mounts (only read-only test input binds), no published
ports, and no external network. The driver used an allowlisted environment,
local synthetic credentials and separate Azurite logs/metrics containers.
The paired run had an outer 135 s timeout with a 20 s kill grace. Cleanup
removed its labeled containers and volumes; no production access was used.

The combined monitoring ceilings were 1,280 MiB (per process, not one
cgroup): Prometheus 128, Thanos sidecar 96, Store 128, Query 96, Compactor
128, Loki 192, Alloy 128 and Grafana 384 MiB. App and Azurite ceilings were
128 and 192 MiB, respectively, excluded from monitoring-only totals. This
is a positive control with generous headroom, not the 256 MiB RAM /
512 MiB swap production slice.

This setup uses the PR configuration as the starting point, with loopback
listeners, distinct ports for the Thanos processes, a dummy Alloy scrape
(3 s interval, 1 s timeout) and fixture log tail, Grafana plugin downloads
disabled, and Azurite instead of Azure. Loki needed an explicit loopback
memberlist and query frontend advertise address; without the latter it
exited because it could not discover a usable interface. Grafana had
`GOMAXPROCS=2` in the final run. The fixture uses tmpfs instead of the
production storage path. The production Compose file was not run.

## Startup sample (partial readiness)

In the run without a readiness wait and before setting Grafana's
`GOMAXPROCS`, the baseline completed 1,150 requests and the monitored window
1,145, with no errors; p95 was 62.522 and 62.802 ms respectively. The maximum
sampled sum of monitoring `memory.current` was **593.96 MiB** while services
were still starting. The fixture metric and log were queryable, but Loki
reported 503 on readiness and Grafana exited with code 2. The exit cause
was not captured. Counter deltas that span the Grafana exit are invalid;
this run does not establish simultaneous readiness.

## Paired load result after readiness

The readiness probe passed 18.844 s after the driver had issued all starts.
It passed again after load: Alloy, Prometheus, all four Thanos roles, Loki
and authenticated Grafana returned HTTP 200; anonymous Grafana returned
401. Prometheus exposed the single fixture metric series, and a direct Loki
query returned the fixture log. No Grafana dashboard concurrency or history
query workload was driven during the app test.

| 20 s window | HTTP 200 | Errors | p50 (ms) | p95 (ms) | p99 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| App only | 1,172 | 0 | 26.138 | 62.182 | 63.283 |
| App and monitoring | 1,176 | 0 | 26.231 | 62.255 | 64.378 |

The maximum sum of the eight monitoring containers' sampled
`memory.current` was **604.49 MiB**, or 671.62 MiB with the app and
Azurite. These are sums of near-contemporaneous readings, not peaks of a
single aggregate cgroup. The per-container cgroup peaks, from launch,
were:

| Process | Peak (MiB) |
| --- | ---: |
| Prometheus | 55.11 |
| Thanos sidecar | 17.44 |
| Thanos Store | 20.68 |
| Thanos Query | 18.06 |
| Thanos Compactor | 18.68 |
| Loki | 55.87 |
| Alloy | 53.33 |
| Grafana | 384.00 |

No container OOM kills or swap were observed in this run. From first to
last monitored sample (0.001–18.843 s into the load window), the eight
monitoring cgroups charged 0.520 CPU seconds, 0.43 MiB reads and zero
block writes. From process launch to the final sample they charged
11.960 CPU seconds and 341.31 MiB reads (334.00 MiB from Grafana), with
no block writes reported. The latter is an I/O counter result for this
tmpfs-based test; it does not imply that telemetry had no writes. The
run ended with zero containers and volumes bearing its unique label.

This single fixed-order pair establishes runtime readiness and fixture
ingestion alongside a light synthetic app workload at the stated caps.
Its memory reading exceeds the production slice's 256 MiB RAM ceiling;
the actual behavior under that aggregate cap, swap and a two-vCPU host
was not measured. The test did not validate seven-day retention, completed
metric block or Loki chunk upload and query from Azure Blob, cold-cache
queries, dashboard concurrency, production disk I/O or rollback. Keep #67
and #76 open. The next capacity test should use an aggregate cap and a
representative app workload in an isolated environment, starting with
lower-footprint monitoring settings.
