"""PostgreSQL and Redis signals (#74): collected by Alloy, alerted when missing."""

import json
import re
import stat
import tempfile
import unittest
from pathlib import Path

from test_monitoring_alerts import BASE_ENV, compose_config, prepare

ROOT = Path(__file__).resolve().parents[1]
ALLOY = (ROOT / "monitoring/config.alloy").read_text()
RULES = (ROOT / "monitoring/rules/vstore-alerts.yml").read_text()


def component(kind, name):
    match = re.search(r'^' + re.escape(kind) + r' "' + name + r'" \{\n(.*?)^\}', ALLOY, re.S | re.M)
    assert match, f"{kind} {name} missing"
    return match.group(1)


class DatastoreCollectionTests(unittest.TestCase):
    def test_alloy_exports_and_scrapes_postgres_and_redis(self):
        self.assertIn("local.file.postgres_dsn.content", component("prometheus.exporter.postgres", "postgres"))
        self.assertIn('sys.env("MONITORING_REDIS_ADDRESS")', component("prometheus.exporter.redis", "redis"))
        for job in ("postgres", "redis"):
            scrape = component("prometheus.scrape", job)
            self.assertIn("prometheus.remote_write.local.receiver", scrape)
            # Built-in exporters label targets job="integrations/...", which wins over
            # job_name; the job must be rewritten before scraping, as for "host".
            self.assertIn(f"discovery.relabel.{job}_targets.output", scrape)
            relabel = component("discovery.relabel", f"{job}_targets")
            self.assertIn(f"prometheus.exporter.{job}.{job}.targets", relabel)
            self.assertIn(f'replacement = "{job}"', relabel)
        dsn = component("local.file", "postgres_dsn")
        self.assertIn('"/run/secrets/postgres-dsn"', dsn)
        self.assertIn("is_secret = true", dsn)

    def test_alloy_gets_the_dsn_secret_and_redis_address(self):
        alloy = compose_config()["services"]["alloy"]
        self.assertIn("postgres-dsn", [s["source"] for s in alloy["secrets"]])
        self.assertEqual(alloy["environment"]["MONITORING_REDIS_ADDRESS"], "127.0.0.1:6379")


class DatastoreSecretTests(unittest.TestCase):
    def test_without_dsn_prepare_writes_a_placeholder_that_reports_down(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "private"
            result = prepare(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            dsn = (directory / "postgres-dsn").read_text()
            self.assertIn("127.0.0.1:1/", dsn)
            self.assertEqual(stat.S_IMODE((directory / "postgres-dsn").stat().st_mode), 0o600)

    def test_dsn_is_stored_privately_and_never_printed(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "private"
            dsn = "postgresql://vstore_monitor:private-test-pass@127.0.0.1:5432/app?sslmode=disable"
            result = prepare(directory, MONITORING_POSTGRES_DSN=dsn)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((directory / "postgres-dsn").read_text(), dsn)
            self.assertNotIn("private-test-pass", result.stdout + result.stderr)

    def test_superuser_or_malformed_dsn_is_rejected(self):
        for dsn in ("postgresql://postgres:x@127.0.0.1:5432/app", "not a dsn", "postgresql://u:p@h/db\nx"):
            with self.subTest(dsn=dsn), tempfile.TemporaryDirectory() as parent:
                directory = Path(parent) / "private"
                result = prepare(directory, MONITORING_POSTGRES_DSN=dsn)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(directory.exists())
                self.assertNotIn(":x@", result.stdout + result.stderr)


class DatastoreAlertTests(unittest.TestCase):
    def test_missing_datastore_signals_alert(self):
        for name, expr in (("VStorePostgresDown", "pg_up"), ("VStoreRedisDown", "redis_up")):
            with self.subTest(alert=name):
                block = RULES.split(f"- alert: {name}\n", 1)[1].split("- alert:", 1)[0]
                self.assertIn(expr, block)
                self.assertIn("absent(", block, "a missing exporter must not look healthy")
                self.assertRegex(block, r"for: 5m")


if __name__ == "__main__":
    unittest.main()
