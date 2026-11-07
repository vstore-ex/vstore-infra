#!/usr/bin/env python3
"""Prove Azure-backed metric history via the real sidecar and Store Gateway."""

import argparse
import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import smoke_monitoring as smoke


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    args = parser.parse_args()
    smoke.ENV_FILE = args.env_file
    # This destructive fixture drill may only address our isolated emulator project.
    config, project, blob = smoke.local_blob_fixture()
    container = blob.get_container_client("vstore-obs-metrics")
    old = int(time.time()) - 8 * 3600
    uid = str(int(time.time()))
    expression = 'vstore_blob_history_probe{run="' + uid + '"}'
    prometheus_image = config["services"]["prometheus"]["image"]
    upload_name = project + "-history-upload"
    source_stopped = False
    try:
        with tempfile.TemporaryDirectory(prefix="obs-history-") as directory:
            path = Path(directory)
            (path / "input.prom").write_text(
                "# TYPE vstore_blob_history_probe gauge\n"
                + expression
                + " 7 "
                + str(old)
                + "\n# EOF\n"
            )
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "-v",
                    directory + ":/history",
                    "--entrypoint",
                    "/bin/promtool",
                    prometheus_image,
                    "tsdb",
                    "create-blocks-from",
                    "openmetrics",
                    "/history/input.prom",
                    "/history/blocks",
                ],
                check=True,
            )
            blocks = [
                p.name
                for p in (path / "blocks").iterdir()
                if (p / "meta.json").exists()
            ]
            assert blocks

            def old_value():
                result = smoke.get(
                    smoke.address("query", 10902)
                    + "/api/v1/query?"
                    + urllib.parse.urlencode(
                        {
                            "query": expression,
                            "time": str(old + 1),
                            "partial_response": "false",
                        }
                    )
                )
                values = result["data"]["result"]
                return values and values[0]["value"][1] == "7"

            assert not old_value(), "History must not exist before upload"
            smoke.compose(
                "run",
                "-d",
                "--no-deps",
                "--name",
                upload_name,
                "-v",
                directory + "/blocks:/history",
                "sidecar",
                "sidecar",
                "--prometheus.url=http://prometheus:9090",
                "--tsdb.path=/history",
                "--objstore.config-file=/run/secrets/thanos.yml",
                "--http-address=0.0.0.0:10902",
                "--grpc-address=0.0.0.0:10901",
            )
            smoke.eventually(
                lambda: all(
                    container.get_blob_client(block + "/meta.json").exists()
                    for block in blocks
                )
            )
            subprocess.run(
                ["docker", "stop", upload_name], check=True, stdout=subprocess.DEVNULL
            )
            # No source Prometheus or sidecar may satisfy the historical query.
            smoke.compose("stop", "sidecar", "prometheus")
            source_stopped = True
            smoke.eventually(old_value)
            smoke.compose("restart", "store", "query")
            smoke.eventually(old_value)
            auth = (
                "Basic "
                + base64.b64encode(b"admin:local-grafana-test-password").decode()
            )
            url = (
                smoke.address("grafana", 3000)
                + "/api/datasources/proxy/uid/vstore-metrics/api/v1/query?"
                + urllib.parse.urlencode(
                    {
                        "query": expression,
                        "time": str(old + 1),
                        "partial_response": "false",
                    }
                )
            )
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"Authorization": auth}), timeout=20
            ) as response:
                assert json.load(response)["data"]["result"][0]["value"][1] == "7"
            print(
                "PASS: 8-hour-old metric uploaded by real sidecar and read from Azure Blob via Store Gateway after source shutdown and store/query restart"
            )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", upload_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if source_stopped:
            smoke.compose("start", "prometheus", "sidecar")


if __name__ == "__main__":
    main()
