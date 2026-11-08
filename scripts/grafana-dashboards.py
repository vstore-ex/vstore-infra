#!/usr/bin/env python3
"""Vendor pinned grafana.com community dashboards into monitoring/grafana/dashboards/Community.

    python3 scripts/grafana-dashboards.py import [--from DIR]

Each dashboard is fetched at a fixed revision (or read from DIR/<id>.json), checked
against its SHA-256, rewritten to the provisioned datasource uids and saved with a
stable uid and a link to its source. To update one, change its revision and hash.
"""

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "monitoring/grafana/dashboards/Community"
METRICS = {"type": "prometheus", "uid": "vstore-metrics"}
LOGS = {"type": "loki", "uid": "vstore-logs"}
GRAFANA = {"type": "grafana", "uid": "-- Grafana --"}


def logs_for_services(dashboard):
    """Logs / App selects by `job` and parses logfmt; VStore logs carry `service`."""
    text = json.dumps(dashboard)
    text = text.replace("label_values(job)", "label_values(service)")
    text = text.replace('{job=\\"$app\\"}', '{service=\\"$app\\"}')
    text = text.replace(" | logfmt", "")
    return json.loads(text)


DASHBOARDS = {
    # id: (revision, sha256, file, uid, datasource, extra fixup)
    1860: (46, "e58ddc5dc535d4c901f94da9648bc3e3d5ae7b6f78e98e185bce8b289961cea3",
           "node-exporter-full.json", "community-node-exporter", METRICS, None),
    19792: (6, "96348d7c68e6d29ced3ba9a8da4358b8605be6815be52daf6d8be85a44f94971",
            "cadvisor.json", "community-cadvisor", METRICS, None),
    3662: (2, "fa7b22f3f75836f195195f9fb5f3b58120106cee46a59c16e3cd13e661cc3384",
           "prometheus-overview.json", "community-prometheus", METRICS, None),
    9578: (4, "fec7022812aa4e344a288988ac460b04a1b078e527931f90b03e722df7575b9a",
           "alertmanager.json", "community-alertmanager", METRICS, None),
    13639: (2, "d9d454928a0803513442c860e7c37ef5db885b6e6246bbaed685bc7240543480",
            "logs-app.json", "community-logs-app", LOGS, logs_for_services),
}
# Panels whose queries need Kubernetes metrics that a Docker host never has.
UNAVAILABLE = re.compile(r"\bkube_")


def is_variable_ref(value):
    return isinstance(value, str) and value.startswith("$")


def rewrite_datasource(value, default):
    if value is None:
        return None
    if value in ("-- Grafana --", "grafana") or (isinstance(value, dict) and value.get("uid") in ("-- Grafana --", "grafana")):
        return GRAFANA
    if isinstance(value, dict) and value.get("type") == "datasource":
        return GRAFANA
    if is_variable_ref(value) or (isinstance(value, dict) and is_variable_ref(value.get("uid"))):
        return dict(default)
    if isinstance(value, dict) and value.get("type") in ("prometheus", "loki"):
        return dict(METRICS if value["type"] == "prometheus" else LOGS)
    return dict(default)


def rewrite(node, default):
    if isinstance(node, dict):
        if "datasource" in node:
            node["datasource"] = rewrite_datasource(node["datasource"], default)
            if node["datasource"] is None:
                del node["datasource"]
        for value in node.values():
            rewrite(value, default)
    elif isinstance(node, list):
        for value in node:
            rewrite(value, default)


def drop_unavailable(panels):
    kept = []
    for panel in panels:
        exprs = " ".join(t.get("expr", "") for t in panel.get("targets", []))
        if exprs and UNAVAILABLE.search(exprs):
            continue
        if "panels" in panel:
            panel["panels"] = drop_unavailable(panel["panels"])
        kept.append(panel)
    return kept


def vendor(dashboard_id, raw):
    revision, digest, name, uid, default, fixup = DASHBOARDS[dashboard_id]
    actual = hashlib.sha256(raw).hexdigest()
    if actual != digest:
        raise SystemExit(f"{dashboard_id}: SHA-256 {actual} does not match pinned {digest}")
    dashboard = json.loads(raw)
    for key in ("__inputs", "__requires", "__elements"):
        dashboard.pop(key, None)
    templating = dashboard.get("templating", {}).get("list", [])
    dashboard.setdefault("templating", {})["list"] = [v for v in templating if v.get("type") != "datasource"]
    rewrite(dashboard, default)
    dashboard["panels"] = drop_unavailable(dashboard.get("panels", []))
    if fixup:
        dashboard = fixup(dashboard)
    dashboard["id"] = None
    dashboard["uid"] = uid
    dashboard["editable"] = False
    source = f"https://grafana.com/grafana/dashboards/{dashboard_id}"
    dashboard["links"] = [l for l in dashboard.get("links", []) if l.get("url") != source] + [
        {"title": f"Source: grafana.com {dashboard_id} rev {revision}", "type": "link",
         "url": source, "targetBlank": True}]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n")
    print(f"vendored {dashboard_id} rev {revision} -> {OUT.relative_to(ROOT) / name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["import"])
    parser.add_argument("--from", dest="source", type=Path, help="directory with <id>.json downloads")
    args = parser.parse_args()
    for dashboard_id, (revision, *_rest) in DASHBOARDS.items():
        if args.source:
            raw = (args.source / f"{dashboard_id}.json").read_bytes()
        else:
            url = f"https://grafana.com/api/dashboards/{dashboard_id}/revisions/{revision}/download"
            with urllib.request.urlopen(url, timeout=30) as response:
                raw = response.read()
        vendor(dashboard_id, raw)


if __name__ == "__main__":
    sys.exit(main())
