# Grafana cold-start resource screen — 2026-11-06

Issue #67; monitoring configuration revision `21b6ef3`. Grafana from that
revision (13.3.0) and the 12.4.14 prototype candidate were tested with the
immutable image references in `tests/probe_grafana_footprint.py`.

Local Docker Desktop 29.8.0, native Linux arm64, cgroup v2, 8 VM cores, roughly
3.82 GiB VM RAM. Each trial used a fresh container, 0.5 CPU, no swap, no network,
and disabled plugin preinstallation. Probes queried `GET /api/org` inside the
container. These were cold-start checks on a shared workstation with variable
background load, so the observations do not rank startup speeds.

| RAM ceiling | Run | 13.3.0 authenticated 200 within window | 12.4.14 authenticated 200 within window |
| --- | --- | --- | --- |
| 128 MiB | Initial, two alternating 25 s pairs | 0/2 | 0/2 |
| 128 MiB | Initial follow-up, one 15 s pair | 0/1 | 0/1 |
| 128 MiB | Independent review, one 15 s pair | 0/1 | 1/1 (10.22 s) |
| 128 MiB | Corrected probe, one 15 s pair | 0/1 | 1/1 (7.003 s) |
| 384 MiB | Initial control, one 20 s pair | 1/1 (15.84 s) | 1/1 (10.69 s) |
| 384 MiB | Independent review, one 20 s pair | 0/1 | 1/1 (11.85 s) |
| 384 MiB | Corrected probe, one 25 s pair | 1/1 (13.64 s) | 1/1 (10.83 s) |

For the 384 MiB controls, both images returned authenticated 200 and anonymous
401 in the separate checks **after** the observation window, even when an
image had not returned 200 within the window. In the independent and corrected
128 MiB runs, only 12.4.14 returned these responses. Both images hit the 128 MiB ceiling:
the first four trials recorded 55,716–119,064 `memory.events:max` events.
No container OOM kills were observed. The low-cap follow-up verified
`memory.max=134217728` and `memory.swap.max=0` in both containers.

Under this test profile, 13.3.0 did not reach authenticated readiness at
128 MiB in the recorded trials, while 12.4.14 reached it in some runs but not
consistently. These observations do not recommend a version change. The
previous tuned prototype used different Go runtime settings. Next, compare
bounded runtime settings under a matched positive control and then measure the
whole stack with application traffic. This screen has no production, dashboard,
Blob history, retention or recovery evidence.
