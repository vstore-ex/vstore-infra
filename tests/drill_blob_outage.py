#!/usr/bin/env python3
"""Measure how long a Blob outage can last before metric or log history is lost.

Runs the production Prometheus/Thanos sidecar/Loki configuration against a local
Azurite emulator, with Prometheus block length and local retention divided by
--scale (default 10: 2h blocks and 6h retention become 12m and 36m). Azurite is
stopped once the first block has shipped and restarted after --outage minutes.

Every local block is tracked from creation to deletion. A block deleted before
the sidecar could upload it is lost history. Measured times are multiplied by
--scale to estimate production values. Logs are pushed to Loki throughout and
then read back after Loki restarts with an empty local volume, so every line
counted came from Blob.

Only the randomly named project created here is touched; it is removed in
`finally`. Needs Docker and azure-storage-blob (no production credentials).
"""

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_BLOCK_MIN = 120
PRODUCTION_RETENTION_MIN = 360


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def analyse(blocks, shipped, outage_start, outage_end):
    """Summarise block lifetimes. Times are Unix seconds; block ranges are ms.

    `blocks` maps ULID -> {min, max, seen, gone}; `gone` is None if still local.
    """
    lost = sorted(
        (b for u, b in blocks.items() if u not in shipped and b["gone"] is not None),
        key=lambda b: b["min"],
    )
    result = {
        "blocks_seen": len(blocks),
        "blocks_shipped": sum(1 for u in blocks if u in shipped),
        "lost_blocks": len(lost),
        "lost_ranges_ms": [[b["min"], b["max"]] for b in lost],
        "lost_data_seconds": sum((b["max"] - b["min"]) / 1000 for b in lost),
        # Time from outage start to the first deletion of a never-uploaded block.
        "tolerance_seconds": (
            min(b["gone"] for b in lost) - outage_start if lost else None
        ),
        "tolerance_lower_bound_seconds": None if lost else outage_end - outage_start,
        "blocks_created_during_outage": sum(
            1 for b in blocks.values() if outage_start <= b["seen"] <= outage_end
        ),
    }
    return result


class Drill:
    def __init__(self, scale, outage_min, directory):
        self.scale = scale
        self.block_min = PRODUCTION_BLOCK_MIN / scale
        self.retention_min = PRODUCTION_RETENTION_MIN / scale
        self.outage_min = outage_min
        self.project = "vstore-obs-" + uuid.uuid4().hex[:8]
        self.dir = Path(directory)
        self.key = base64.b64encode(b"x" * 32).decode()
        self.conn = (
            "DefaultEndpointsProtocol=http;AccountName=localaccount;"
            f"AccountKey={self.key};BlobEndpoint=http://azurite:10000/localaccount;"
        )
        self.loki_port = free_port()
        self.events = []

    def log(self, message):
        stamp = datetime.now(UTC).strftime("%H:%M:%S")
        self.events.append(f"{stamp} {message}")
        print(f"{stamp} {message}", flush=True)

    def compose(self, *args, check=True):
        files = [ROOT / "monitoring/compose.yml", ROOT / "tests/fixtures/monitoring/compose.yml"]
        if sys.platform == "darwin":
            files.append(ROOT / "tests/fixtures/monitoring/desktop.yml")
        files.append(self.dir / "drill.yml")
        command = ["docker", "compose", "--env-file", str(self.env_file)]
        for path in files:
            command += ["-f", str(path)]
        done = subprocess.run(command + list(args), text=True, capture_output=True)
        if check and done.returncode:
            raise RuntimeError(f"compose {' '.join(args)} failed: {done.stderr[-800:]}")
        return done.stdout.strip()

    def prepare(self):
        secrets = self.dir / "secrets"
        env = {
            **os.environ,
            "METRICS_TOKEN": "local-test-only-token",
            "MONITORING_GRAFANA_PASSWORD": "local-grafana-test-password",
            "MONITORING_LOGS_CONNECTION_STRING": self.conn,
            "MONITORING_METRICS_CONNECTION_STRING": self.conn,
            "MONITORING_LOGS_CONTAINER": "vstore-obs-logs",
            "MONITORING_METRICS_CONTAINER": "vstore-obs-metrics",
        }
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/monitoring.py"), "prepare", "--directory", str(secrets)],
            env=env, check=True, stdout=subprocess.DEVNULL,
        )
        self.env_file = secrets / "compose.env"
        with self.env_file.open("a") as out:
            out.write(
                f"OBS_AZURITE_ACCOUNTS=localaccount:{self.key}\n"
                f"OBS_BACKEND_CONTEXT={self.dir}\n"
                f"OBS_FIXTURE_DOCKERFILE={ROOT}/tests/fixtures/monitoring/Dockerfile\n"
                f"OBS_CONFIG_ROOT={ROOT}/monitoring\n"
                f"COMPOSE_PROJECT_NAME={self.project}\n"
                f"MONITORING_PROMETHEUS_PORT={free_port()}\n"
                f"MONITORING_LOKI_PORT={self.loki_port}\n"
                f"MONITORING_GRAFANA_PORT={free_port()}\n"
            )
        block, retention = f"{self.block_min:g}m", f"{self.retention_min:g}m"
        # Only block length and local retention differ from monitoring/compose.yml.
        (self.dir / "drill.yml").write_text(
            "services:\n  prometheus:\n    command:\n"
            + "".join(
                f"      - {flag}\n"
                for flag in (
                    "--config.file=/etc/prometheus/prometheus.yml",
                    "--storage.tsdb.path=/prometheus",
                    f"--storage.tsdb.retention.time={retention}",
                    "--storage.tsdb.retention.size=512MB",
                    f"--storage.tsdb.min-block-duration={block}",
                    f"--storage.tsdb.max-block-duration={block}",
                    "--web.enable-remote-write-receiver",
                )
            )
        )

    def blob(self):
        from azure.storage.blob import BlobServiceClient

        endpoint = self.compose("port", "azurite", "10000")
        assert endpoint.startswith("127.0.0.1:"), endpoint
        local = self.conn.replace("azurite:10000", endpoint)
        return BlobServiceClient.from_connection_string(local)

    def create_containers(self):
        from azure.core.exceptions import ResourceExistsError

        deadline = time.monotonic() + 120
        while True:
            try:
                client = self.blob()
                for name in ("vstore-obs-logs", "vstore-obs-metrics"):
                    try:
                        client.create_container(name)
                    except ResourceExistsError:
                        pass
                return
            except Exception:  # noqa: BLE001 - emulator still starting
                if time.monotonic() > deadline:
                    raise
                time.sleep(2)

    def local_blocks(self):
        script = 'for d in /prometheus/01*; do [ -f "$d/meta.json" ] && cat "$d/meta.json" && echo "@@"; done; true'
        out = self.compose("exec", "-T", "prometheus", "sh", "-c", script)
        blocks = {}
        for chunk in out.split("@@"):
            if chunk.strip():
                meta = json.loads(chunk)
                blocks[meta["ulid"]] = (meta["minTime"], meta["maxTime"])
        return blocks

    def shipped(self):
        out = self.compose(
            "exec", "-T", "prometheus", "sh", "-c", "cat /prometheus/thanos.shipper.json 2>/dev/null || true"
        )
        return set(json.loads(out).get("uploaded") or []) if out else set()

    def blob_blocks(self):
        container = self.blob().get_container_client("vstore-obs-metrics")
        ranges = []
        for item in container.list_blobs():
            if item.name.endswith("/meta.json") and item.name.count("/") == 1:
                meta = json.loads(container.download_blob(item.name).readall())
                ranges.append((meta["minTime"], meta["maxTime"], meta["ulid"]))
        return sorted(ranges)

    def push_log(self, run, seq):
        body = json.dumps(
            {"streams": [{"stream": {"drill": "blob-outage", "run": run},
                          "values": [[str(time.time_ns()), f"seq={seq}"]]}]}
        ).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.loki_port}/loki/api/v1/push",
            data=body, headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=10).close()

    def read_logs(self, run, start_ns):
        query = urllib.parse.urlencode({
            "query": f'{{drill="blob-outage",run="{run}"}}',
            "start": str(start_ns), "end": str(time.time_ns()), "limit": "5000",
        })
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.loki_port}/loki/api/v1/query_range?{query}", timeout=30
        ) as response:
            result = json.load(response)["data"]["result"]
        return {int(line.split("=")[1]) for stream in result for _, line in stream["values"]}

    def memory(self):
        ids = self.compose("ps", "-q", "prometheus", "sidecar", "loki").split()
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}", *ids],
            text=True, capture_output=True,
        ).stdout
        return {line.split()[0].rsplit("-", 2)[-2]: line.split()[1] for line in out.splitlines() if line}

    def run(self):
        self.prepare()
        self.compose("up", "-d", "azurite")
        self.create_containers()
        self.compose("up", "-d", "prometheus", "sidecar", "loki")
        self.log(f"project {self.project}: blocks {self.block_min:g}m, retention {self.retention_min:g}m, "
                 f"outage {self.outage_min:g}m (scale 1:{self.scale:g})")

        blocks, peak = {}, {}

        def sample():
            now = time.time()
            current = self.local_blocks()
            for ulid, (lo, hi) in current.items():
                if ulid not in blocks:
                    blocks[ulid] = {"min": lo, "max": hi, "seen": now, "gone": None}
                    self.log(f"block {ulid[-6:]} cut ({(hi - lo) / 60000:.0f} min of data)")
            for ulid, block in blocks.items():
                if ulid not in current and block["gone"] is None:
                    block["gone"] = now
                    self.log(f"block {ulid[-6:]} deleted by local retention")
            return self.shipped()

        deadline = time.time() + 60 * (2 * self.block_min + 10)
        while not sample():
            if time.time() > deadline:
                raise RuntimeError("no block shipped before the outage")
            time.sleep(20)
        self.log("first block shipped to Blob")

        run, seq, pushed, push_errors = uuid.uuid4().hex[:8], 0, set(), 0
        log_start = time.time_ns()
        self.compose("stop", "azurite")
        outage_start = time.time()
        self.log("Blob outage started (Azurite stopped)")
        shipped_before = sample()
        last_push = 0
        while time.time() < outage_start + 60 * self.outage_min:
            if time.time() - last_push >= 10:
                last_push = time.time()
                seq += 1
                try:
                    self.push_log(run, seq)
                    pushed.add(seq)
                except Exception:  # noqa: BLE001 - recorded as ingestion loss
                    push_errors += 1
            if int(time.time() - outage_start) % 60 < 10:
                for name, usage in self.memory().items():
                    peak[name] = max(peak.get(name, "0"), usage, key=to_mib)
            sample()
            time.sleep(10)
        self.compose("start", "azurite")
        outage_end = time.time()
        self.log("Blob outage ended (Azurite started)")

        deadline = time.time() + 600
        while True:
            shipped = sample()
            remaining = [u for u, b in blocks.items() if b["gone"] is None and u not in shipped]
            if not remaining or time.time() > deadline:
                break
            time.sleep(15)
        self.log(f"after recovery {len(remaining)} local block(s) still unshipped")

        # Logs: flush, then restart Loki with no local state so reads come from Blob.
        urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{self.loki_port}/flush", method="POST"), timeout=30
        ).close()
        time.sleep(60)
        self.compose("rm", "-sf", "loki")
        subprocess.run(["docker", "volume", "rm", f"{self.project}_loki"], check=True, capture_output=True)
        self.compose("up", "-d", "--force-recreate", "init", "loki")
        deadline, read = time.time() + 300, set()
        while time.time() < deadline:
            try:
                read = self.read_logs(run, log_start)
                if read >= pushed:
                    break
            except Exception:  # noqa: BLE001 - Loki restarting
                pass
            time.sleep(10)
        self.log(f"logs: pushed {len(pushed)}, push errors {push_errors}, read back from Blob {len(read & pushed)}")

        summary = analyse(blocks, shipped, outage_start, outage_end)
        blob_ranges = self.blob_blocks()
        gaps = [
            [prev[1], nxt[0]] for prev, nxt in zip(blob_ranges, blob_ranges[1:]) if nxt[0] - prev[1] > 60_000
        ]
        scale = self.scale

        def production(seconds):
            return None if seconds is None else round(seconds * scale / 3600, 2)

        return {
            "date": datetime.now(UTC).isoformat(),
            "environment": f"{sys.platform} Docker; production images and configs from monitoring/",
            "production": False,
            "scale": scale,
            "fixture": {"block_minutes": self.block_min, "retention_minutes": self.retention_min,
                        "outage_minutes": self.outage_min},
            "metrics": {**summary,
                        "shipped_before_outage": len(shipped_before),
                        "blob_blocks_after_recovery": len(blob_ranges),
                        "blob_gaps_ms": gaps,
                        "production_estimate_hours": {
                            "tolerance": production(summary["tolerance_seconds"]),
                            "tolerance_at_least": production(summary["tolerance_lower_bound_seconds"]),
                            "lost_history": production(summary["lost_data_seconds"]),
                        }},
            "logs": {"pushed": len(pushed), "push_errors": push_errors,
                     "read_back_from_blob": len(read & pushed),
                     "missing": sorted(pushed - read)[:50]},
            "peak_memory": peak,
            "events": self.events,
        }


def to_mib(usage):
    number = float("".join(c for c in usage if c.isdigit() or c == ".") or 0)
    return number * {"G": 1024, "M": 1, "k": 1 / 1024}.get(usage.rstrip("iB")[-1:], 1 / 1048576)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scale", type=float, default=10)
    parser.add_argument("--outage", type=float, help="outage minutes (default: past the expected loss point)")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    outage = args.outage or (PRODUCTION_RETENTION_MIN + 1.5 * PRODUCTION_BLOCK_MIN) / args.scale + 6
    with tempfile.TemporaryDirectory(prefix="obs-outage-") as directory:
        drill = Drill(args.scale, outage, directory)
        try:
            report = drill.run()
        finally:
            drill.compose("down", "--volumes", "--remove-orphans", check=False)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    metrics = report["metrics"]["production_estimate_hours"]
    print(json.dumps({"production_estimate_hours": metrics, "logs": report["logs"]}, indent=2))


if __name__ == "__main__":
    main()
