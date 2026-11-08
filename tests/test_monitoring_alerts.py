import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {
    "METRICS_TOKEN": "local-test-only",
    "MONITORING_GRAFANA_PASSWORD": "local-password-only",
    "MONITORING_LOGS_CONNECTION_STRING": "AccountName=logs;AccountKey=dummy",
    "MONITORING_METRICS_CONNECTION_STRING": "AccountName=metrics;AccountKey=dummy",
    "MONITORING_LOGS_CONTAINER": "vstore-logs",
    "MONITORING_METRICS_CONTAINER": "vstore-metrics",
}


def compose_config(*overlays):
    files = ["-f", str(ROOT / "monitoring/compose.yml")]
    for overlay in overlays:
        files += ["-f", str(ROOT / "monitoring" / overlay)]
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(ROOT / ".env.example"), *files,
         "config", "--format", "json"],
        env={**os.environ, "MONITORING_SECRETS_DIR": "/tmp/test-secrets"},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def prepare(directory, **extra):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/monitoring.py"), "prepare", "--directory", str(directory)],
        env={**os.environ, **BASE_ENV, **extra}, capture_output=True, text=True, check=False,
    )


class AlertingStackTests(unittest.TestCase):
    def test_alertmanager_is_private_bounded_and_persistent(self):
        services = compose_config()["services"]
        am = services["alertmanager"]
        self.assertEqual(am.get("ports", []), [])
        self.assertIn("monitoring", am["networks"])
        self.assertLessEqual(int(am["mem_limit"]), 128 * 1024 * 1024)
        self.assertTrue(any(v.get("target") == "/alertmanager" and v.get("type") == "volume"
                            for v in am["volumes"]))
        self.assertIn("--cluster.listen-address=", am["command"])

    def test_every_monitoring_service_is_confined_by_the_production_overlay(self):
        base = set(compose_config()["services"])
        guarded = compose_config("production.yml")["services"]
        self.assertEqual(set(guarded), base)
        for name, service in guarded.items():
            self.assertEqual(service.get("cgroup_parent"), "vstore-monitoring.slice", name)

    def test_prometheus_evaluates_rules_and_notifies_alertmanager(self):
        config = (ROOT / "monitoring/prometheus.yml").read_text()
        self.assertIn("/etc/prometheus/rules/*.yml", config)
        self.assertIn('"alertmanager:9093"', config)
        prometheus = compose_config()["services"]["prometheus"]
        self.assertTrue(any(v.get("target") == "/etc/prometheus/rules" for v in prometheus["volumes"]))

    def test_collector_exports_pressure_for_the_io_alert(self):
        alloy = (ROOT / "monitoring/config.alloy").read_text()
        for line in alloy.splitlines():
            if line.strip().startswith(("enable_collectors", "set_collectors")):
                self.assertIn('"pressure"', line)

    def test_repository_config_has_no_real_recipients(self):
        config = (ROOT / "monitoring/alertmanager/alertmanager.yml").read_text()
        for marker in ("telegram_configs", "webhook_configs", "email_configs", "bot_token", "chat_id"):
            self.assertNotIn(marker, config)


class TelegramPreparationTests(unittest.TestCase):
    def test_without_telegram_the_repository_config_is_used(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "private"
            result = prepare(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((directory / "alertmanager.yml").exists())
            self.assertNotIn("MONITORING_ALERTMANAGER_CONFIG", (directory / "compose.env").read_text())

    def test_telegram_receiver_is_private_and_token_is_never_printed(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "private"
            result = prepare(directory, MONITORING_TELEGRAM_BOT_TOKEN="123:private-test-token",
                             MONITORING_TELEGRAM_CHAT_ID="-1001234567890")
            self.assertEqual(result.returncode, 0, result.stderr)
            config = directory / "alertmanager.yml"
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            text = config.read_text()
            self.assertIn("telegram_configs", text)
            self.assertIn("chat_id: -1001234567890", text)
            self.assertIn("send_resolved: true", text)
            self.assertIn(f'MONITORING_ALERTMANAGER_CONFIG="{config}"',
                          (directory / "compose.env").read_text())
            self.assertNotIn("private-test-token", result.stdout + result.stderr)

    def test_half_configured_telegram_fails_closed(self):
        for extra in ({"MONITORING_TELEGRAM_BOT_TOKEN": "123:x"},
                      {"MONITORING_TELEGRAM_CHAT_ID": "-100"},
                      {"MONITORING_TELEGRAM_BOT_TOKEN": "123:x", "MONITORING_TELEGRAM_CHAT_ID": "chat"}):
            with self.subTest(extra=list(extra)), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp) / "private"
                result = prepare(directory, **extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
