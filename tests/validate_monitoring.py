#!/usr/bin/env python3
"""Validate monitoring with official binaries in network-disabled containers."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="vstore-obs-validate-") as temp:
        private = Path(temp) / "secrets"
        env = {
            **os.environ,
            "METRICS_TOKEN": "local-validation-only",
            "MONITORING_GRAFANA_PASSWORD": "local-validation-only",
            "MONITORING_LOGS_CONNECTION_STRING": "DefaultEndpointsProtocol=http;AccountName=localaccount;AccountKey=eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=;BlobEndpoint=http://127.0.0.1:9/localaccount;",
            "MONITORING_METRICS_CONNECTION_STRING": "DefaultEndpointsProtocol=http;AccountName=localaccount;AccountKey=eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=;BlobEndpoint=http://127.0.0.1:9/localaccount;",
            "MONITORING_LOGS_CONTAINER": "vstore-obs-logs",
            "MONITORING_METRICS_CONTAINER": "vstore-obs-metrics",
        }
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/monitoring.py"),
                "prepare",
                "--directory",
                str(private),
            ],
            env=env,
            check=True,
        )
        telegram = Path(temp) / "telegram"
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/monitoring.py"), "prepare", "--directory", str(telegram)],
            env={**env, "MONITORING_TELEGRAM_BOT_TOKEN": "123456:validation-only",
                 "MONITORING_TELEGRAM_CHAT_ID": "-1000000000000"},
            check=True,
        )
        config = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    str(private / "compose.env"),
                    "-f",
                    str(ROOT / "monitoring/compose.yml"),
                    "config",
                    "--format",
                    "json",
                ],
                text=True,
            )
        )
        checks = [
            (
                "prometheus",
                "/bin/promtool",
                ["check", "config", "/etc/monitoring/prometheus.yml"],
            ),
            ("prometheus", "/bin/promtool", ["check", "rules", "/etc/monitoring/rules/vstore-alerts.yml"]),
            ("prometheus", "/bin/promtool", ["test", "rules", "/etc/monitoring/rules-tests/vstore-alerts.test.yml"]),
            ("alertmanager", "/bin/amtool", ["check-config", "/etc/monitoring/alertmanager/alertmanager.yml"]),
            # Private Telegram variant from `prepare`, with dummy values.
            ("alertmanager", "/bin/amtool", ["check-config", "/run/telegram/alertmanager.yml"]),
            ("alertmanager", "/bin/amtool", [
                "template", "render",
                "--template.glob=/etc/monitoring/alertmanager/telegram.tmpl",
                '--template.text={{ template "vstore.telegram" . }}',
            ]),
            ("alloy", "/bin/alloy", ["validate", "/etc/monitoring/config.alloy"]),
            (
                "loki",
                "/usr/bin/loki",
                ["-config.file=/run/secrets/loki.yml", "-verify-config=true"],
            ),
        ]
        for service, entry, flags in checks:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "-v",
                    str(ROOT / "monitoring") + ":/etc/monitoring:ro",
                    "-v",
                    str(private) + ":/run/secrets:ro",
                    "-v",
                    str(telegram) + ":/run/telegram:ro",
                    # Same rule path as the running Prometheus, so `check config` loads the rules.
                    "-v",
                    str(ROOT / "monitoring/rules") + ":/etc/prometheus/rules:ro",
                    "-e",
                    "MONITORING_PROMETHEUS_URL=http://127.0.0.1:9/api/v1/write",
                    "-e",
                    "MONITORING_LOKI_URL=http://127.0.0.1:9/loki/api/v1/push",
                    "-e",
                    "MONITORING_API_ADDRESS=127.0.0.1:9",
                    "--entrypoint",
                    entry,
                    config["services"][service]["image"],
                    *flags,
                ],
                check=True,
            )
    print("PASS: official Prometheus, rule-test, Alertmanager, Alloy and Loki validators")


if __name__ == "__main__":
    main()
