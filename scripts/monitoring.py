#!/usr/bin/env python3
"""Prepare private monitoring configuration without provisioning or starting anything."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare(directory):
    keys = (
        "METRICS_TOKEN",
        "MONITORING_GRAFANA_PASSWORD",
        "MONITORING_LOGS_CONNECTION_STRING",
        "MONITORING_METRICS_CONNECTION_STRING",
        "MONITORING_LOGS_CONTAINER",
        "MONITORING_METRICS_CONTAINER",
    )
    values = {key: os.environ.get(key, "") for key in keys}
    if any(
        not value.strip() or "\n" in value or "\r" in value for value in values.values()
    ):
        raise ValueError(
            "All monitoring credentials/container names must be nonempty single-line values"
        )
    # The pinned legacy Loki Azure client injects SharedKey authentication even
    # for SAS connection strings (upstream #21596). Never emit unusable secrets.
    if any(
        part.split("=", 1)[0].strip().lower() == "sharedaccesssignature"
        for part in values["MONITORING_LOGS_CONNECTION_STRING"].split(";")
    ):
        raise ValueError("The pinned Loki client does not support SAS authentication")
    logs, metrics = (
        values["MONITORING_LOGS_CONTAINER"],
        values["MONITORING_METRICS_CONTAINER"],
    )
    if (
        logs == metrics
        or "--" in logs
        or "--" in metrics
        or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", name)
            for name in (logs, metrics)
        )
    ):
        raise ValueError("Use two distinct valid private Blob container names")
    # Generated compose.env runs every nonroot service as the preparing UID.
    if os.getuid() == 0 or os.getgid() == 0:
        raise ValueError("Run prepare as the unprivileged monitoring owner, not root")
    path = Path(directory)
    if (
        any(char in str(path) for char in ("\n", "\r", "\x00"))
        or not path.is_absolute()
        or path.exists()
        or path.is_symlink()
    ):
        raise ValueError(
            "Use a new absolute private directory; existing configuration is never overwritten"
        )
    loki = (ROOT / "monitoring/loki.template.yml").read_text()
    loki = loki.replace(
        "@LOGS_CONNECTION_STRING@",
        json.dumps(values["MONITORING_LOGS_CONNECTION_STRING"]),
    )
    loki = loki.replace("@LOGS_CONTAINER@", json.dumps(logs))
    account = dict(
        part.split("=", 1)
        for part in values["MONITORING_METRICS_CONNECTION_STRING"].split(";")
        if "=" in part
    ).get("AccountName", "")
    if not re.fullmatch(r"[a-z0-9]{3,24}", account):
        raise ValueError("Metrics connection string must include a valid AccountName")
    thanos = (
        "type: AZURE\nconfig:\n  storage_create_container: false\n  storage_account: "
        + json.dumps(account)
        + "\n"
    )
    thanos += (
        "  storage_connection_string: "
        + json.dumps(values["MONITORING_METRICS_CONNECTION_STRING"])
        + "\n"
    )
    thanos += "  container: " + json.dumps(metrics) + "\n"
    files = {
        "metrics-token": values["METRICS_TOKEN"],
        "grafana-password": values["MONITORING_GRAFANA_PASSWORD"],
        "loki.yml": loki,
        "thanos.yml": thanos,
        "compose.env": f"MONITORING_SECRETS_DIR={json.dumps(str(path))}\nMONITORING_UID={os.getuid()}\nMONITORING_GID={os.getgid()}\n",
    }
    path.mkdir(mode=0o700)
    for name, value in files.items():
        fd = os.open(path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(value)
    print(
        "Prepared private monitoring configuration; no resources created or services started"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--directory", required=True)
    args = parser.parse_args()
    try:
        prepare(args.directory)
    except (ValueError, OSError) as error:
        # Configuration failures never echo credential values.
        print(
            f"Monitoring preparation failed: {type(error).__name__}; verify inputs and directory permissions",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
