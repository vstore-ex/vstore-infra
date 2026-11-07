#!/usr/bin/env python3
"""Read-only assertions against an explicitly started, isolated monitoring fixture."""

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = None


def compose(*args):
    command = [
        "docker",
        "compose",
        "--env-file",
        str(ENV_FILE),
        "-f",
        str(ROOT / "monitoring/compose.yml"),
        "-f",
        str(ROOT / "tests/fixtures/monitoring/compose.yml"),
    ]
    if sys.platform == "darwin":
        command += ["-f", str(ROOT / "tests/fixtures/monitoring/desktop.yml")]
    return subprocess.check_output(command + list(args), text=True).strip()


def address(service, port):
    return "http://" + compose("port", service, str(port))


def get(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def eventually(check, seconds=120):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except Exception as error:  # noqa: BLE001 - poll external service startup failures
            last = type(error).__name__
        time.sleep(2)
    raise AssertionError(f"Runtime assertion timed out ({last})")


def local_blob_fixture():
    """One safety policy for every mutating emulator drill."""
    from azure.storage.blob import BlobServiceClient

    config = json.loads(compose("config", "--format", "json"))
    project = config["name"]
    assert project.startswith("vstore-obs-")
    private = Path(config["secrets"]["thanos.yml"]["file"]).parent
    for name in ("thanos.yml", "loki.yml"):
        assert (
            "BlobEndpoint=http://azurite:10000/localaccount"
            in (private / name).read_text()
        )
    endpoint = compose("port", "azurite", "10000")
    assert endpoint.startswith("127.0.0.1:")
    key = base64.b64encode(b"x" * 32).decode()
    client = BlobServiceClient.from_connection_string(
        f"DefaultEndpointsProtocol=http;AccountName=localaccount;AccountKey={key};BlobEndpoint=http://{endpoint}/localaccount;"
    )
    return config, project, client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    global ENV_FILE
    ENV_FILE = args.env_file
    backend = address("backend", 8000)
    assert get(backend + "/")["message"]
    query = address("query", 10902)

    def series(expr):
        return get(query + "/api/v1/query?" + urllib.parse.urlencode({"query": expr}))[
            "data"
        ]["result"]

    eventually(lambda: series('vstore_http_requests_total{job="api"}'))
    eventually(lambda: series('node_cpu_seconds_total{job="host"}'))
    eventually(lambda: series('container_memory_working_set_bytes{job="containers"}'))
    eventually(lambda: series('probe_success{job="api-health"}'))
    # Upload/retention health must be observable before a Blob outage loses blocks.
    eventually(lambda: series('up{job="thanos-sidecar"} == 1'))
    eventually(lambda: series('up{job="thanos-compact"} == 1'))
    if sys.platform == "linux":
        eventually(
            lambda: series(
                'container_memory_working_set_bytes{job="containers",name!=""}'
            )
        )
        # Compare a physical host interface, not merely any nonempty netdev series.
        interfaces = [
            name
            for name in Path("/sys/class/net").iterdir()
            if (name / "device").exists()
        ]
        assert interfaces, (
            "Linux fixture needs a physical host interface to prove network namespace coverage"
        )
        device = interfaces[0].name
        for direction, field in (("receive", 0), ("transmit", 8)):
            expr = (
                "node_network_"
                + direction
                + '_bytes_total{job="host",device="'
                + device
                + '"}'
            )
            observed = eventually(lambda expr=expr: series(expr))
            rows = {
                line.split(":")[0].strip(): line.split(":")[1].split()
                for line in Path("/proc/1/net/dev").read_text().splitlines()[2:]
            }
            assert 0 <= float(observed[0]["value"][1]) <= int(rows[device][field])
        print(
            "PASS: both network directions match a physical host interface in /proc/1/net/dev"
        )
    else:
        print(
            "LIMITATION: Docker Desktop does not prove per-container cgroup coverage; Linux verification required"
        )
    print("PASS: real backend and collector series are queryable through Thanos")
    trace = "obs-smoke-" + uuid.uuid4().hex
    request = urllib.request.Request(
        backend + "/api/v1/games/00000000-0000-0000-0000-000000000001",
        headers={"X-Request-ID": trace},
    )
    try:
        urllib.request.urlopen(request, timeout=10)
        raise AssertionError("Fixture database intentionally has no application tables")
    except urllib.error.HTTPError as response:
        assert response.code == 500
        assert (
            response.headers["X-Request-ID"]
            == json.load(response)["request_id"]
            == trace
        )
    loki = address("loki", 3100)

    def error_logs():
        expr = '{service="backend"} | json | request_id="' + trace + '"'
        result = get(
            loki
            + "/loki/api/v1/query_range?"
            + urllib.parse.urlencode(
                {
                    "query": expr,
                    "start": str(int((time.time() - 600) * 1e9)),
                    "end": str(int(time.time() * 1e9)),
                }
            )
        )
        for stream in result["data"]["result"]:
            for _, line in stream["values"]:
                entry = json.loads(line)
                if entry.get("event") == "http_error" and "Traceback" in entry.get(
                    "stack_trace", ""
                ):
                    return entry

    eventually(error_logs)
    auth = "Basic " + base64.b64encode(b"admin:local-grafana-test-password").decode()

    def grafana(path):
        request = urllib.request.Request(
            address("grafana", 3000) + path, headers={"Authorization": auth}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    dashboard = eventually(lambda: grafana("/api/dashboards/uid/vstore-overview"))
    assert len(dashboard["dashboard"]["panels"]) >= 12
    network = next(
        panel
        for panel in dashboard["dashboard"]["panels"]
        if "VM network" in panel["title"]
    )
    assert len(network["targets"]) == 2
    for direction, target in zip(
        ("receive", "transmit"), network["targets"], strict=True
    ):
        assert "node_network_" + direction + "_bytes_total" in target["expr"]
        path = (
            "/api/datasources/proxy/uid/vstore-metrics/api/v1/query?"
            + urllib.parse.urlencode({"query": target["expr"]})
        )
        eventually(lambda path=path: grafana(path)["data"]["result"])
    failures = next(
        panel
        for panel in dashboard["dashboard"]["panels"]
        if panel["title"].startswith("Metric block upload failures")
    )
    path = (
        "/api/datasources/proxy/uid/vstore-metrics/api/v1/query?"
        + urllib.parse.urlencode({"query": failures["targets"][0]["expr"]})
    )
    eventually(lambda: grafana(path)["data"]["result"])
    expr = urllib.parse.urlencode({"query": 'vstore_http_requests_total{job="api"}'})
    assert grafana("/api/datasources/proxy/uid/vstore-metrics/api/v1/query?" + expr)[
        "data"
    ]["result"]
    print(
        "PASS: provisioned Grafana dashboard/datasource works; controlled 500 stack trace is searchable by returned request ID in Loki"
    )


if __name__ == "__main__":
    main()
