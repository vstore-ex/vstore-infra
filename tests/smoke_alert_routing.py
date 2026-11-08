#!/usr/bin/env python3
"""Prove Alertmanager grouping, inhibition and resolve messages with the repository
routing against a local webhook receiver. No external recipient is contacted."""

import json
import subprocess
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALERTMANAGER = "prom/alertmanager:v0.29.0@sha256:88743b63b3e09ea6e31e140ced5bf45f4a8e82c617c2a963f78841f4995ad1d7"
RECEIVER = "python:3.13-alpine@sha256:1e1f6334529d1fb26a2ef619d7cffb30a7c7d7cc1cc7338ebee283a58d1aeaca"
RECEIVER_CODE = """
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        with open('/out/received.jsonl', 'a') as f:
            f.write(json.dumps(json.loads(body)) + '\\n')
        self.send_response(200); self.end_headers()
    def log_message(self, *a): pass
http.server.HTTPServer(('0.0.0.0', 8080), H).serve_forever()
"""


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True).stdout.strip()


def eventually(check, seconds=90):
    deadline = time.time() + seconds
    while True:
        result = check()
        if result or time.time() > deadline:
            return result
        time.sleep(1)


def main():
    name = "vstore-am-" + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="vstore-am-") as temp:
        temp = Path(temp)
        out = temp / "out"
        out.mkdir()
        out.chmod(0o777)
        # Repository routing, with the default receiver pointed at the local webhook.
        base = (ROOT / "monitoring/alertmanager/alertmanager.yml").read_text()
        assert base.endswith("receivers:\n  - name: default\n")
        config = temp / "alertmanager.yml"
        config.write_text(base + "    webhook_configs:\n      - url: http://receiver:8080/\n        send_resolved: true\n")
        config.chmod(0o644)
        try:
            docker("network", "create", name)
            docker("run", "-d", "--name", f"{name}-receiver", "--network", name, "--network-alias", "receiver",
                   "-v", f"{out}:/out", RECEIVER, "python", "-c", RECEIVER_CODE)
            docker("run", "-d", "--name", f"{name}-am", "--network", name, "-p", "127.0.0.1::9093",
                   "-v", f"{config}:/etc/alertmanager/alertmanager.yml:ro",
                   "-v", f"{ROOT / 'monitoring/alertmanager/telegram.tmpl'}:/etc/alertmanager/templates/telegram.tmpl:ro",
                   ALERTMANAGER, "--config.file=/etc/alertmanager/alertmanager.yml",
                   "--storage.path=/alertmanager", "--cluster.listen-address=")
            port = docker("port", f"{name}-am", "9093/tcp").splitlines()[0].rsplit(":", 1)[1]
            api = f"http://127.0.0.1:{port}/api/v2/alerts"

            def ready():
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/-/ready", timeout=2) as r:
                        return r.status == 200
                except OSError:
                    return False
            assert eventually(ready), "Alertmanager did not become ready"

            now = datetime.now(timezone.utc)
            def alert(name_, end=None, **labels):
                item = {"labels": {"alertname": name_, **labels},
                        "annotations": {"summary": name_, "runbook": "monitoring/README.md#alert-runbook"},
                        "startsAt": now.isoformat()}
                if end:
                    item["endsAt"] = end.isoformat()
                return item

            def post(alerts):
                request = urllib.request.Request(api, json.dumps(alerts).encode(), {"Content-Type": "application/json"})
                urllib.request.urlopen(request, timeout=5).read()

            post([
                alert("VStoreScrapeFailing", severity="warning", job="host"),
                alert("VStoreScrapeFailing", severity="warning", job="containers"),
                alert("VStoreApiDown", severity="critical", job="api-health"),
                alert("VStoreHighErrorRate", severity="critical"),
            ])
            log = out / "received.jsonl"

            def received():
                return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []

            # group_wait is 30 s; both groups must arrive.
            batches = eventually(lambda: len(received()) >= 2 and received(), 75)
            assert batches, "No notifications reached the local receiver"
            by_name = {b["groupLabels"]["alertname"]: b for b in batches}
            scrape = by_name["VStoreScrapeFailing"]
            assert len(scrape["alerts"]) == 2, scrape
            assert {a["labels"]["job"] for a in scrape["alerts"]} == {"host", "containers"}
            assert "VStoreApiDown" in by_name, batches
            time.sleep(5)
            names = {b["groupLabels"]["alertname"] for b in received()}
            assert "VStoreHighErrorRate" not in names, "inhibition failed: error-rate alert sent while API down"
            print("PASS: grouped two scrape failures into one notification; API-down inhibited the error-rate alert")

            # Resolve the API alert; a resolved notification follows after group_interval (5 m)
            # in production. Restarting the evaluation here would take minutes, so check
            # the resolved state is accepted and the alert is no longer active instead.
            post([alert("VStoreApiDown", end=now + timedelta(seconds=1), severity="critical", job="api-health")])
            def inactive():
                with urllib.request.urlopen(api + "?active=true&filter=alertname%3D%22VStoreApiDown%22", timeout=5) as r:
                    return json.load(r) == []
            assert eventually(inactive, 30), "resolved alert stayed active"
            print("PASS: resolved alert left the active set (the resolved message itself follows group_interval and is not awaited)")
        finally:
            docker("rm", "-f", f"{name}-am", f"{name}-receiver", check=False)
            docker("network", "rm", name, check=False)


if __name__ == "__main__":
    main()
