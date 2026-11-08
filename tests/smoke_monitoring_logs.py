#!/usr/bin/env python3
"""Prove Loki Blob reads with empty local state, then Blob outage recovery."""

import argparse
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import smoke_monitoring as smoke


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    args = parser.parse_args()
    smoke.ENV_FILE = args.env_file
    _config, project, blob = smoke.local_blob_fixture()
    container = blob.get_container_client("vstore-obs-logs")
    backend = smoke.address("backend", 8000)
    trace = "blob-log-" + uuid.uuid4().hex

    def cause_error(request_id):
        request = urllib.request.Request(
            backend + "/api/v1/games/00000000-0000-0000-0000-000000000001",
            headers={"X-Request-ID": request_id},
        )
        try:
            urllib.request.urlopen(request, timeout=10)
            raise AssertionError("Expected fixture DB error")
        except urllib.error.HTTPError as error:
            assert error.code == 500 and error.headers["X-Request-ID"] == request_id

    def logs(base, request_id):
        expr = '{service="backend"} | json | request_id="' + request_id + '"'
        result = smoke.get(
            base
            + "/loki/api/v1/query_range?"
            + urllib.parse.urlencode(
                {
                    "query": expr,
                    "start": str(int((time.time() - 1800) * 1e9)),
                    "end": str(int(time.time() * 1e9)),
                }
            )
        )
        return any(
            "stack_trace" in line
            for stream in result["data"]["result"]
            for _, line in stream["values"]
        )

    cause_error(trace)
    smoke.eventually(lambda: logs(smoke.address("loki", 3100), trace))
    with urllib.request.urlopen(
        urllib.request.Request(smoke.address("loki", 3100) + "/flush", data=b""),
        timeout=30,
    ) as response:
        assert response.status < 300
    smoke.eventually(
        lambda: any(item.name.startswith("index/") for item in container.list_blobs()),
        seconds=180,
    )
    clean_name = project + "-clean-loki"
    stopped = False
    try:
        with tempfile.TemporaryDirectory(prefix=project + "-loki-") as directory:
            smoke.compose("stop", "loki")
            stopped = True
            smoke.compose(
                "run",
                "-d",
                "--no-deps",
                "--name",
                clean_name,
                "-p",
                "127.0.0.1::3100",
                "-v",
                directory + ":/loki",
                "loki",
            )

            def clean_logs():
                port = subprocess.check_output(
                    ["docker", "port", clean_name, "3100/tcp"], text=True
                ).strip()
                assert port.startswith("127.0.0.1:")
                return logs("http://" + port, trace)

            smoke.eventually(clean_logs)
            print(
                "PASS: request-ID stack trace read from Blob with an empty Loki WAL/index/cache"
            )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", clean_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if stopped:
            smoke.compose("start", "loki")
    # Azure outage must not interrupt fresh metrics or require app restarts.
    blob_stopped = False
    try:
        smoke.compose("stop", "azurite")
        blob_stopped = True
        during = "blob-outage-" + uuid.uuid4().hex
        cause_error(during)
        expr = urllib.parse.urlencode(
            {
                "query": 'vstore_http_requests_total{job="api"}',
                "partial_response": "false",
            }
        )
        smoke.eventually(
            lambda: smoke.get(smoke.address("query", 10902) + "/api/v1/query?" + expr)[
                "data"
            ]["result"]
        )
    finally:
        if blob_stopped:
            smoke.compose("start", "azurite")
    smoke.eventually(lambda: logs(smoke.address("loki", 3100), during))
    print(
        "PASS: fresh metrics continue during Blob outage; correlated log is available after recovery"
    )


if __name__ == "__main__":
    main()
