# Minimum VStore observability stack

Research for [Choose the minimum viable VStore observability stack](https://github.com/vstore-ex/vstore-infra/issues/64).

## Decision

Use a five-service, self-hosted stack on the existing Linux VM:

1. **Grafana Alloy** is the single collection agent. Its embedded exporters collect host, Docker container, PostgreSQL, and HTTP probe metrics; its Docker log source tails container logs.
2. **Prometheus** stores metrics and evaluates recording and alerting rules.
3. **Alertmanager** groups, inhibits, silences, and routes notifications.
4. **Loki**, in single-binary mode with filesystem storage, stores searchable container logs.
5. **Grafana** provides provisioned Prometheus/Loki data sources and version-controlled dashboards.

This is the smallest maintainable stack that keeps Prometheus-native rule evaluation and the already-approved Alertmanager notification model while avoiding four standalone exporter containers. Alloy embeds the `node_exporter`, cAdvisor, `postgres_exporter`, and `blackbox_exporter` implementations behind its Unix, cAdvisor, PostgreSQL, and blackbox components ([Unix exporter](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.unix/), [cAdvisor exporter](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.cadvisor/), [PostgreSQL exporter](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.postgres/), [blackbox exporter](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.blackbox/)). Alloy can discover Docker containers and forward their logs to Loki while retaining read positions across restarts ([Docker log source](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.docker/)).

Alloy scrapes the embedded exporters and backend metrics, then remote-writes samples to Prometheus over the private monitoring network. Prometheus exposes that ingestion endpoint only when `--web.enable-remote-write-receiver` is set ([Prometheus command-line reference](https://prometheus.io/docs/prometheus/latest/command-line/prometheus/)); no receiver or monitoring UI is published on a host-wide interface.

## Coverage

| Requirement | Source and signal |
| --- | --- |
| VM resources | Alloy Unix exporter: CPU, memory, load, filesystem, disk, and network metrics from host-mounted procfs/sysfs/rootfs. |
| Container resources/state | Alloy cAdvisor exporter: per-container CPU, memory, filesystem, and network series; Docker discovery supplies stable service labels. |
| Backend availability | Alloy blackbox HTTP probe through the internal proxy path, plus Prometheus `up` for the application metrics endpoint. |
| Backend latency and HTTP 5xx | A small backend change exposes Prometheus counters and histograms labelled only by normalized route, method, and status class. Raw paths, user IDs, and request IDs must not be metric labels. |
| PostgreSQL health | Alloy PostgreSQL exporter using a dedicated least-privilege monitoring role; the existing `/health` database check remains the user-path liveness signal. |
| Dashboards | Grafana provisions Prometheus/Loki data sources and dashboard JSON from Git. Grafana officially supports file provisioning for both data sources and dashboards ([provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/)). |
| Alerts | Prometheus rules use persistence windows and send firing alerts to Alertmanager; Alertmanager handles grouping, inhibition, silences, and receivers ([Prometheus alerting overview](https://prometheus.io/docs/alerting/latest/overview/), [alerting rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)). |
| Correlated logs | Alloy sends Docker logs to Loki with low-cardinality labels such as Compose service and environment. Backend structured log fields include `request_id`, status, normalized route, and duration so operators can query an HTTP 500 by the response `X-Request-ID`. |

The backend already creates and returns `X-Request-ID` and injects `request_id` into log records, but the current default log formatting does not establish a structured, searchable contract (`app/core/middleware.py` in `vstore`). The infrastructure currently retains only three 10 MiB `json-file` segments per container and has no metric store, dashboard, or alert evaluator ([Compose logging anchor](../docker-compose.yml)). The implementation therefore needs both infrastructure services and a narrow backend instrumentation/log-format change.

## Deployment shape and bounds

- Keep all monitoring HTTP ports internal to the Compose network or bound to `127.0.0.1`; do not expose Grafana, Prometheus, Alertmanager, Loki, or Alloy publicly. Reach Grafana through the existing administrative access path or an SSH tunnel.
- Pin images to reviewed versions/digests and provision configuration from the repository. Persist Prometheus, Loki, Grafana, Alertmanager, and Alloy position data in named volumes so an intentional VM shutdown does not discard state.
- Use a 30-second initial scrape interval and explicitly cap retention by both time and size. Prometheus supports both limits and deletes the oldest blocks when the size limit is reached; its documentation recommends reserving 15-20% of the allocated disk for compaction headroom ([Prometheus storage](https://prometheus.io/docs/prometheus/latest/storage/)). Exact limits should be chosen only after the VM capacity ticket measures available RAM and disk.
- Run Loki as one monolithic process with TSDB plus filesystem storage. Grafana documents single-binary mode for simple setups and filesystem as the easiest local store ([Loki architecture](https://grafana.com/docs/loki/latest/get-started/architecture/), [filesystem store](https://grafana.com/docs/loki/latest/operations/storage/filesystem/)). Enable Compactor retention: Loki otherwise retains logs forever, and filesystem storage does not protect the host from a full disk ([Loki retention](https://grafana.com/docs/loki/latest/operations/storage/retention/)).
- Keep stream labels low-cardinality; store `request_id` inside parsed log fields rather than as a Loki label.
- Do not use Promtail. It reached end of life on March 2, 2026; Grafana directs new log collection to Alloy ([Promtail status](https://grafana.com/docs/loki/latest/send-data/promtail/)).

## Security and operational trade-offs

Alloy consolidation reduces containers and duplicate scrape processes, but host/container collection is privileged. Grafana documents that the embedded cAdvisor collector on Docker needs the Docker socket, host mounts, and privileged mode; Docker socket access is effectively root-equivalent ([Alloy cAdvisor Docker requirements](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.cadvisor/)). Treat this as an explicit infrastructure exception: no public port, read-only mounts wherever supported, a fixed image digest, no application-network access beyond the exact scrape/database destinations, and no receiver that accepts arbitrary remote configuration.

The stack runs on the monitored VM. When the VM is intentionally off, no evaluator runs and no alert fires, which matches the approved planned-shutdown policy. The same design **cannot detect an unplanned total VM or network outage**. Detecting that failure requires an evaluator outside the VM and is deliberately deferred because the current scope prohibits new paid services or cloud resources.

Notification receiver addresses and credentials remain outside Git. The repository should contain safe templates plus a local webhook test; activating real recipients is a separate production action.

## Alternatives rejected

- **Separate node_exporter, cAdvisor, postgres_exporter, blackbox_exporter, and a log agent:** equivalent signal quality but at least four more services and more version/configuration surfaces than Alloy.
- **Promtail for logs:** unsupported after its 2026 end-of-life.
- **Grafana-managed alerting without Alertmanager:** one fewer container, but it abandons the approved Prometheus-rule/Alertmanager model and couples notification evaluation to the dashboard service.
- **VictoriaMetrics or a distributed Loki deployment:** useful at larger scale, but adds a second query/rule model or extra services without evidence that this single-VM workload needs them.
- **OpenTelemetry tracing/Tempo:** explicitly outside the minimum scope; add only after metrics and correlated logs demonstrate a concrete diagnostic gap.

## Implementation consequences

1. Measure the VM operating envelope before setting Compose memory limits and data-retention caps.
2. Add the monitoring services and Git-provisioned configuration in `vstore-infra`, with deterministic config validation and local smoke tests.
3. Add normalized HTTP metrics and structured request logs in `vstore`; avoid high-cardinality labels and sensitive payloads.
4. Prove one end-to-end path: trigger a synthetic HTTP 500, observe the 5xx metric and alert state, then find the matching stack trace in Loki using the returned request ID.
5. Prove restart/offline behavior and disk bounds locally or on staging; do not deploy to production or activate real receivers without renewed authorization.
