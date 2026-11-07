import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "monitoring/grafana/dashboards/overview.json"


def panel_exprs():
    panels = json.loads(DASHBOARD.read_text())["panels"]
    return {p["title"]: [t["expr"] for t in p.get("targets", [])] for p in panels}


class GuardBudgetTests(unittest.TestCase):
    def budget(self, root):
        return subprocess.run(
            ["bash", str(root / "scripts/monitoring-guard.sh"), "budget"],
            capture_output=True, text=True, check=False,
        )

    def test_guard_reads_budget_from_the_reviewed_slice_unit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "scripts").mkdir()
            (root / "monitoring/systemd").mkdir(parents=True)
            shutil.copy(ROOT / "scripts/monitoring-guard.sh", root / "scripts")
            unit = root / "monitoring/systemd/vstore-monitoring.slice"
            text = (ROOT / "monitoring/systemd/vstore-monitoring.slice").read_text()
            unit.write_text(re.sub(r"(?m)^MemoryMax=.*$", "MemoryMax=300M", text))
            result = self.budget(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"memory.max={300 * 1024 * 1024}", result.stdout)

    def test_reviewed_budget_covers_measured_peak_without_swap(self):
        result = self.budget(ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = dict(line.split("=", 1) for line in result.stdout.split())
        # ThinkPad 2026-11-06 sampled aggregate peak: 604.49 MiB.
        self.assertGreater(int(values["memory.max"]), 605 * 1024 * 1024)
        # Shared-disk swap caused the 2026-11-02 application health timeouts.
        self.assertEqual(values["memory.swap.max"], "0")


class BlobUploadVisibilityTests(unittest.TestCase):
    def test_prometheus_scrapes_thanos_upload_and_retention_components(self):
        config = (ROOT / "monitoring/prometheus.yml").read_text()
        self.assertIn('"sidecar:10902"', config)
        self.assertIn('"compact:10902"', config)

    def test_dashboard_shows_metric_upload_age_as_unknown_until_first_upload(self):
        exprs = [e for es in panel_exprs().values() for e in es]
        self.assertTrue(any(
            "thanos_objstore_bucket_last_successful_upload_time" in e and "> 0" in e
            for e in exprs
        ), exprs)
        self.assertTrue(any("thanos_shipper_upload_failures_total" in e for e in exprs))


class DashboardQueryTests(unittest.TestCase):
    def test_vm_network_excludes_container_virtual_interfaces(self):
        exprs = panel_exprs()["VM network receive/transmit bytes per second"]
        self.assertEqual(len(exprs), 2)
        for expr in exprs:
            matcher = re.search(r'device!~"([^"]+)"', expr.replace('\\"', '"'))
            self.assertIsNotNone(matcher, expr)
            pattern = re.compile(matcher.group(1))
            for virtual in ("lo", "veth1a2b3c", "docker0", "br-0123456789ab"):
                self.assertTrue(pattern.fullmatch(virtual), virtual)
            self.assertFalse(pattern.fullmatch("eth0"))

    def test_logs_panel_selects_any_application_service(self):
        dashboard = json.loads(DASHBOARD.read_text())
        variables = {v["name"]: v for v in dashboard["templating"]["list"]}
        self.assertEqual(variables["service"]["datasource"]["uid"], "vstore-logs")
        self.assertTrue(variables["service"]["includeAll"])
        logs = [p for p in dashboard["panels"] if p["type"] == "logs"]
        self.assertEqual(len(logs), 1)
        self.assertIn('service=~"$service"', logs[0]["targets"][0]["expr"])


class PrepareTests(unittest.TestCase):
    def test_prepare_refuses_root_owned_runtime(self):
        spec = importlib.util.spec_from_file_location("monitoring", ROOT / "scripts/monitoring.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        env = {
            "METRICS_TOKEN": "t", "MONITORING_GRAFANA_PASSWORD": "p",
            "MONITORING_LOGS_CONNECTION_STRING": "AccountName=logs;AccountKey=dummy",
            "MONITORING_METRICS_CONNECTION_STRING": "AccountName=metrics;AccountKey=dummy",
            "MONITORING_LOGS_CONTAINER": "vstore-logs",
            "MONITORING_METRICS_CONTAINER": "vstore-metrics",
        }
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.dict(module.os.environ, env), \
                mock.patch.object(module.os, "getuid", return_value=0):
            directory = Path(temp) / "secrets"
            with self.assertRaises(ValueError):
                module.prepare(str(directory))
            self.assertFalse(directory.exists())


class CollectorPrivilegeTests(unittest.TestCase):
    def test_privileged_collector_declares_no_redundant_capabilities(self):
        compose = (ROOT / "monitoring/compose.yml").read_text()
        alloy = compose.split("\n  alloy:\n", 1)[1].split("\n  grafana:\n", 1)[0]
        self.assertIn("privileged: true", alloy)
        self.assertNotIn("cap_add", alloy)


if __name__ == "__main__":
    unittest.main()
