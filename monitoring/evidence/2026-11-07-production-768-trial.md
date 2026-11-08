# Production trial at the 768 MiB budget — 2026-11-07

Owner-authorized trial on the production VM (`Standard_B2als_v2`, 2 vCPU,
3872 MiB RAM, 2 GiB swap file, Debian, Docker systemd cgroup v2 driver).
Source: PR #77 commit `7c697d1`, exported to the host; private configuration
reused from the 2026-11-02 trial (Loki shared key, Thanos metrics SAS).

## Guard migration

- `check` against the installed 2026-10-30 unit (256 MiB + 512 MiB swap)
  failed with "Memory guard limits do not match reviewed budget".
- `install` refused the different unit. With the slice reporting `populated 0`,
  the old unit was backed up, disabled and removed; `install` then created the
  reviewed unit and `check` passed: `memory.max=805306368`, `memory.swap.max=0`,
  `pids.max=256`, `cpu.max=50000 100000`.
- All eight services ran under `vstore-monitoring.slice` with OOM score +500
  and restart policy `no`.

## Application impact

A host-local probe requested backend `/health` and `/api/v1/games/` on
`127.0.0.1:8080` once per second (10 s timeout). A watchdog would have stopped
the monitoring project on any non-200 response, any response over 1 s, swap
use over 50 MiB or a monitoring OOM kill. It did not trigger.

| | Baseline (5 min) | Monitoring (30 min) |
|---|---|---|
| Samples / errors | 301 / 0 | 1800 / 0 |
| `/health` p50 / p95 / p99 | 4.3 / 6.4 / 7.9 ms | 4.4 / 6.1 / 9.7 ms |
| Games API p50 / p95 / p99 | 5.5 / 8.7 / 14.6 ms | 5.6 / 9.8 / 34.0 ms |
| Lowest MemAvailable | 2787 MiB | 1722 MiB |
| Swap used | 0 | 0 |
| Highest 1-min load | 0.65 | 1.61 |

## Monitoring footprint

- The slice stayed at its ceiling (peak 769 MiB): 552 MiB anonymous memory,
  165 MiB file cache, 29 MiB kernel at the end. The ceiling was reached 29,800
  times; reclaim came from cache. Zero OOM kills; slice memory pressure 0 at
  the end (about 1% full stall during startup).
- Per container at the end: Grafana 270 MiB, Alloy 126, Loki 124, Prometheus 69,
  sidecar 27, Compactor 24, Query 21, Store 18.
- Named volumes: about 570 MB, almost all Grafana (567 MB).
- All scrape targets were up except `api`: the running backend had no
  `METRICS_TOKEN` (later deployments recreated it without the 2026-11-02 overlay).
  The token in the application `.env` matches the collector token, so the root
  Compose change in this PR enables it on the next deployment.
- The sidecar uploaded its first block to the private metrics container:
  `thanos_shipper_uploads_total=1`, `thanos_shipper_upload_failures_total=0`.
  Grafana returned 401 to anonymous requests and reported its database healthy.

## Limits

One 30-minute window with synthetic one-request-per-second load, not peak
traffic. No historical Grafana query older than the local window, seven-day
retention or Blob outage drill on production. The metrics SAS expires on
2026-12-02. Monitoring was left running after the trial.

## Addendum: thrashing after the window

About 20 minutes after the window ended (checked at 22:35–22:40 UTC, after the
#77 deployment recreated the backend), the stack had degraded:

- slice anonymous memory 629 MiB, file cache 25 MiB, `memory.events max` 542,891;
  zero OOM kills;
- Grafana 370 MiB of its 384 MiB cap;
- slice memory pressure `full avg60` 30%; host I/O pressure `some avg60` 90%,
  `full avg60` 73%; load average 9.3 on 2 vCPU;
- Prometheus and sidecar `up=0`; Alloy timed out on the Docker socket and on
  remote write; Loki batches were dropped.

At 22:41 UTC the monitoring project was stopped (volumes kept). One minute
later host I/O pressure `some avg10` was 5%, `/health` answered in 3 ms and every
application container was healthy. Application impact between 22:17 and 22:41
UTC was not measured. Conclusion: 768 MiB without swap is not a sustainable
budget; the stack needs room for page cache as well as process memory.
