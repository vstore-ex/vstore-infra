import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / "monitoring/grafana/dashboards"
SOURCES = ROOT / "monitoring/grafana/provisioning/datasources/sources.yml"


def all_dashboards():
    return sorted(DASHBOARDS.rglob("*.json"))


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


class DashboardSetTests(unittest.TestCase):
    def test_vstore_and_community_dashboards_are_in_separate_folders(self):
        folders = {path.parent.name for path in all_dashboards()}
        self.assertEqual(folders, {"VStore", "Community"})
        community = {json.loads(p.read_text())["title"] for p in (DASHBOARDS / "Community").glob("*.json")}
        for title in ("Node Exporter Full", "cadvisor dashboard", "Prometheus 2.0 Overview",
                      "Alertmanager", "Logs / App"):
            self.assertIn(title, community)
        own = {json.loads(p.read_text())["title"] for p in (DASHBOARDS / "VStore").glob("*.json")}
        self.assertEqual(own, {"VStore overview", "VStore application", "VStore monitoring health"})
        provider = (ROOT / "monitoring/grafana/provisioning/dashboards/dashboards.yml").read_text()
        self.assertIn("foldersFromFilesStructure: true", provider)

    def test_every_datasource_reference_resolves_to_a_provisioned_uid(self):
        uids = set(re.findall(r"uid: ([\w-]+)", SOURCES.read_text()))
        for path in all_dashboards():
            text = path.read_text()
            with self.subTest(dashboard=path.name):
                self.assertNotRegex(text, r"\$\{?(DS_|ds_prometheus|datasource)", "unresolved import input")
                dashboard = json.loads(text)
                self.assertNotIn("__inputs", dashboard)
                for node in walk(dashboard):
                    source = node.get("datasource")
                    if isinstance(source, dict) and source.get("type") not in (None, "datasource", "grafana"):
                        self.assertIn(source.get("uid"), uids, source)
                    elif isinstance(source, str):
                        self.fail(f"untyped datasource reference {source!r}")

    def test_dashboard_uids_are_stable_and_unique(self):
        seen = {}
        for path in all_dashboards():
            dashboard = json.loads(path.read_text())
            uid = dashboard.get("uid")
            self.assertRegex(uid or "", r"^[a-z0-9-]{3,40}$", path.name)
            self.assertNotIn(uid, seen, f"{path.name} and {seen.get(uid)}")
            seen[uid] = path.name
            self.assertIsNone(dashboard.get("id"), path.name)

    def test_community_dashboards_record_their_source(self):
        for path in (DASHBOARDS / "Community").glob("*.json"):
            dashboard = json.loads(path.read_text())
            links = [link.get("url", "") for link in dashboard.get("links", [])]
            with self.subTest(dashboard=path.name):
                self.assertTrue(any(re.match(r"https://grafana\.com/grafana/dashboards/\d+", u) for u in links), links)


class CollectionForDashboardsTests(unittest.TestCase):
    def test_host_collectors_cover_node_exporter_full(self):
        alloy = (ROOT / "monitoring/config.alloy").read_text()
        line = next(l for l in alloy.splitlines() if l.strip().startswith("set_collectors"))
        for collector in ("uname", "diskstats", "vmstat", "stat", "filefd", "sockstat", "netstat", "time", "pressure"):
            self.assertIn(f'"{collector}"', line)

    def test_container_metrics_keep_only_compose_labels(self):
        alloy = (ROOT / "monitoring/config.alloy").read_text()
        self.assertIn("store_container_labels = false", alloy)
        self.assertRegex(alloy, r'allowlisted_container_labels = \["com\.docker\.compose\.project", "com\.docker\.compose\.service"\]')

    def test_every_monitoring_component_is_scraped(self):
        config = (ROOT / "monitoring/prometheus.yml").read_text()
        for target in ("sidecar:10902", "compact:10902", "store:10902", "query:10902",
                       "loki:3100", "grafana:3000", "alertmanager:9093"):
            self.assertIn(f'"{target}"', config)
        self.assertIn('prometheus.exporter.self', (ROOT / "monitoring/config.alloy").read_text())


if __name__ == "__main__":
    unittest.main()
