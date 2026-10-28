import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MonitoringComposeTests(unittest.TestCase):
    def test_opt_in_stack_has_no_public_monitoring_ports(self):
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ROOT / ".env.example"),
                "-f",
                str(ROOT / "monitoring/compose.yml"),
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "MONITORING_SECRETS_DIR": "/private/tmp/test-monitoring-secrets",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        self.assertIn("grafana", services)
        self.assertEqual(services["alloy"]["network_mode"], "host")
        self.assertIn(
            "--server.http.listen-addr=127.0.0.1:12345", services["alloy"]["command"]
        )
        self.assertEqual(
            [name for name, service in services.items() if service.get("privileged")],
            ["alloy"],
        )
        for name, service in services.items():
            for port in service.get("ports", []):
                self.assertEqual(port["host_ip"], "127.0.0.1", name)
            self.assertIn("ALL", service.get("cap_drop", []), name)
            self.assertIn(
                "no-new-privileges:true", service.get("security_opt", []), name
            )
        base = subprocess.check_output(
            ["docker", "compose", "--env-file", ".env.example", "config", "--services"],
            cwd=ROOT,
            text=True,
        )
        self.assertNotIn("grafana", base.splitlines())

    def test_prepare_fails_closed_and_produces_private_runtime_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "secrets"
            env = {
                **os.environ,
                "METRICS_TOKEN": "local-test-only",
                "MONITORING_GRAFANA_PASSWORD": "local-password-only",
                "MONITORING_LOGS_CONNECTION_STRING": "DefaultEndpointsProtocol=https;AccountName=logs;AccountKey=dummy",
                "MONITORING_METRICS_CONNECTION_STRING": "DefaultEndpointsProtocol=https;AccountName=metrics;AccountKey=dummy",
                "MONITORING_LOGS_CONTAINER": "vstore-logs",
                "MONITORING_METRICS_CONTAINER": "vstore-metrics",
            }
            cmd = [
                sys.executable,
                str(ROOT / "scripts/monitoring.py"),
                "prepare",
                "--directory",
                str(directory),
            ]
            result = subprocess.run(
                cmd,
                env={**env, "METRICS_TOKEN": ""},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(directory.exists())
            result = subprocess.run(
                cmd, env=env, text=True, capture_output=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            for name in [
                "metrics-token",
                "grafana-password",
                "thanos.yml",
                "loki.yml",
                "compose.env",
            ]:
                self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)
            self.assertNotIn("AccountKey", result.stdout + result.stderr)
            self.assertIn(
                "storage_create_container: false",
                (directory / "thanos.yml").read_text(),
            )
            self.assertNotIn("@LOGS_", (directory / "loki.yml").read_text())
            # Existing configuration is never overwritten implicitly.
            self.assertNotEqual(
                subprocess.run(
                    cmd, env=env, capture_output=True, check=False
                ).returncode,
                0,
            )


if __name__ == "__main__":
    unittest.main()
