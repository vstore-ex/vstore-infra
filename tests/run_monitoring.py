#!/usr/bin/env python3
"""Run only an ephemeral fixture: real exported backend source plus local Azurite."""

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import smoke_monitoring as smoke
from azure.storage.blob import BlobServiceClient

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-context", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    context = args.backend_context.resolve()
    assert (context / "app/core/observability.py").is_file(), (
        "Export backend PR #58 or its merged revision first"
    )
    assert not (context / ".git").exists(), (
        "Use git archive, not a developer checkout containing private files"
    )
    assert not any(p.name != ".env.example" for p in context.rglob(".env*")), (
        "Fixture source must contain no environment secrets"
    )

    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    grafana_port, prom_port, loki_port = free_port(), free_port(), free_port()
    postgres_port, redis_port = free_port(), free_port()
    # Linux Alloy shares the host network; Docker Desktop Alloy joins the app network.
    datastore = "postgres:5432" if sys.platform == "darwin" else f"127.0.0.1:{postgres_port}"
    redis_address = "redis:6379" if sys.platform == "darwin" else f"127.0.0.1:{redis_port}"
    project = "vstore-obs-" + uuid.uuid4().hex[:8]
    key = base64.b64encode(b"x" * 32).decode()
    conn = f"DefaultEndpointsProtocol=http;AccountName=localaccount;AccountKey={key};BlobEndpoint=http://azurite:10000/localaccount;"
    with tempfile.TemporaryDirectory(prefix=project + "-") as directory:
        secret_dir = Path(directory) / "secrets"
        env = {
            **os.environ,
            "METRICS_TOKEN": "local-test-only-token",
            "MONITORING_GRAFANA_PASSWORD": "local-grafana-test-password",
            "MONITORING_LOGS_CONNECTION_STRING": conn,
            "MONITORING_METRICS_CONNECTION_STRING": conn,
            "MONITORING_LOGS_CONTAINER": "vstore-obs-logs",
            "MONITORING_METRICS_CONTAINER": "vstore-obs-metrics",
            "MONITORING_POSTGRES_DSN": f"postgresql://vstore_monitor:local-test-only@{datastore}/app?sslmode=disable",
        }
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/monitoring.py"),
                "prepare",
                "--directory",
                str(secret_dir),
            ],
            env=env,
            check=True,
        )
        smoke.ENV_FILE = secret_dir / "compose.env"
        with smoke.ENV_FILE.open("a") as out:
            out.write(
                f"OBS_AZURITE_ACCOUNTS=localaccount:{key}\nOBS_BACKEND_CONTEXT={context}\nOBS_FIXTURE_DOCKERFILE={ROOT}/tests/fixtures/monitoring/Dockerfile\nOBS_CONFIG_ROOT={ROOT}/monitoring\nCOMPOSE_PROJECT_NAME={project}\nMONITORING_GRAFANA_PORT={grafana_port}\nMONITORING_PROMETHEUS_PORT={prom_port}\nMONITORING_LOKI_PORT={loki_port}\nOBS_POSTGRES_PORT={postgres_port}\nOBS_REDIS_PORT={redis_port}\nMONITORING_REDIS_ADDRESS={redis_address}\n"
            )
        try:
            smoke.compose("build", "backend")
            smoke.compose("up", "-d", "backend", "azurite", "prometheus", "postgres", "redis")
            emulator = smoke.compose("port", "azurite", "10000")
            assert emulator.startswith("127.0.0.1:")
            local = conn.replace("azurite:10000", emulator)
            blob = BlobServiceClient.from_connection_string(local)
            for name in ("vstore-obs-logs", "vstore-obs-metrics"):
                smoke.eventually(lambda name=name: create_container(blob, name))
            with smoke.ENV_FILE.open("a") as out:
                out.write(
                    "MONITORING_API_ADDRESS="
                    + smoke.compose("port", "backend", "8000")
                    + "\n"
                )
            smoke.compose("up", "-d", "--no-build")
            # Validate with the component CLIs, not a duplicated schema in Python.
            smoke.compose(
                "exec",
                "-T",
                "prometheus",
                "promtool",
                "check",
                "config",
                "/etc/prometheus/prometheus.yml",
            )
            smoke.compose(
                "exec", "-T", "alloy", "alloy", "validate", "/etc/alloy/config.alloy"
            )
            smoke.compose(
                "exec",
                "-T",
                "loki",
                "/usr/bin/loki",
                "-config.file=/run/secrets/loki.yml",
                "-verify-config=true",
            )
            for script in (
                "smoke_monitoring.py",
                "smoke_monitoring_history.py",
                "smoke_monitoring_logs.py",
            ):
                subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "tests" / script),
                        "--env-file",
                        str(smoke.ENV_FILE),
                    ],
                    check=True,
                )
            ids = smoke.compose("ps", "-q").splitlines()
            stats = subprocess.check_output(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids],
                text=True,
            )
            report = {
                "date": datetime.now(UTC).isoformat(),
                "environment": sys.platform,
                "production": False,
                "backend_context": "exported instrumentation revision",
                "project": project,
                "checks": [
                    "live metrics and provisioned Grafana",
                    "request-ID Loki correlation",
                    "8h metric Blob history without source",
                    "restart",
                    "Blob outage/recovery",
                    "log Blob query with empty local state",
                ],
                "docker_stats": [json.loads(line) for line in stats.splitlines()],
                "limitations": [
                    "synthetic fixture load, not proof of production fit",
                    "Docker Desktop is not a Linux host coverage test"
                    if sys.platform == "darwin"
                    else "dedicated temporary Linux fixture; no production adoption",
                ],
            }
            args.report.write_text(json.dumps(report, indent=2) + "\n")
            print("PASS: isolated monitoring fixture; report written")
        finally:
            # Only the randomly named project just created above is removed.
            smoke.compose("down", "--volumes", "--remove-orphans")


def create_container(blob, name):
    from azure.core.exceptions import ResourceExistsError

    try:
        blob.create_container(name)
    except ResourceExistsError:
        pass
    return True


if __name__ == "__main__":
    main()
