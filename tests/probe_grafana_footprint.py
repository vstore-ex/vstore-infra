#!/usr/bin/env python3
"""Measure standalone Grafana cold starts on the local Docker Desktop VM."""

import argparse
import json
from pathlib import Path
import secrets
import signal
import subprocess
import tempfile
import time
import uuid

BASELINE = "grafana/grafana:13.3.0@sha256:488984b716c0862639f91a698adbe2862677a5a07bad42da5f03f3030883b205"
CANDIDATE = "grafana/grafana:12.4.14@sha256:ae509d9ae8c1021ca32ec370a9ca975d40165eda9fccd1e1102d317ff8787cd7"
CGROUP_FILES = (
    "memory.current", "memory.peak", "memory.max", "memory.swap.current", "memory.swap.max",
    "memory.events", "cpu.stat", "io.stat",
)


def command(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=15, **kwargs).stdout.strip()


def docker(*args, **kwargs):
    return command("docker", "--context", "desktop-linux", *args, **kwargs)


def status(name, password=None):
    cfg = 'url="http://127.0.0.1:3000/api/org"\nmax-time=2\n'
    if password:
        cfg += f'user="admin:{password}"\n'
    result = subprocess.run(
        ["docker", "--context", "desktop-linux", "exec", "-i", name,
         "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--config", "-"],
        input=cfg, capture_output=True, text=True, timeout=5,
    )
    return int(result.stdout) if result.stdout.isascii() and result.stdout.isdecimal() else None


def state(name):
    return json.loads(docker("inspect", "--format", "{{json .State}}", name))


def probe(image, seconds, memory_limit, check_cancelled):
    name = f"vstore-grafana-fit-{uuid.uuid4().hex}"
    password = secrets.token_hex(24)
    row = {
        "image": image, "samples": [], "in_window_auth_200_count": 0,
        "first_in_window_auth_200_seconds": None,
    }
    with tempfile.TemporaryDirectory(prefix="vstore-grafana-fit-") as tmp:
        env_file = Path(tmp) / "env"
        env_file.touch(mode=0o600, exist_ok=False)
        env_file.write_text(
            f"GF_SECURITY_ADMIN_PASSWORD={password}\n"
            "GF_USERS_ALLOW_SIGN_UP=false\n"
            "GF_AUTH_ANONYMOUS_ENABLED=false\n"
            "GF_ANALYTICS_REPORTING_ENABLED=false\n"
            "GF_ANALYTICS_CHECK_FOR_UPDATES=false\n"
            "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false\n"
            "GF_PLUGINS_PREINSTALL_DISABLED=true\n"
        )
        try:
            check_cancelled()
            docker(
                "run", "-d", "--pull=never", "--name", name, "--network=none",
                "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
                "--memory", str(memory_limit), "--memory-swap", str(memory_limit), "--cpus", "0.5",
                "--env-file", str(env_file), image,
            )
            check_cancelled()
            started = time.monotonic()
            while time.monotonic() - started < seconds and state(name)["Running"]:
                check_cancelled()
                sample = {"elapsed_seconds": round(time.monotonic() - started, 2)}
                try:
                    raw = docker(
                        "exec", name, "sh", "-ec",
                        'for f do printf "%s=" "$f"; cat "/sys/fs/cgroup/$f"; printf "\\0"; done',
                        "sh", *CGROUP_FILES,
                    )
                except subprocess.CalledProcessError:
                    if state(name)["Running"]:
                        raise
                    break
                sample.update(dict(item.split("=", 1) for item in raw.strip("\0").split("\0")))
                if int(sample["memory.max"]) != memory_limit or int(sample["memory.swap.max"]) != 0:
                    raise RuntimeError("Docker did not enforce the requested memory and swap ceilings")
                check_cancelled()
                sample["auth_http_status"] = status(name, password)
                check_cancelled()
                completed = time.monotonic() - started
                sample["auth_probe_completed_seconds"] = round(completed, 3)
                sample["auth_probe_in_window"] = completed <= seconds
                if sample["auth_http_status"] == 200 and sample["auth_probe_in_window"]:
                    row["in_window_auth_200_count"] += 1
                    if row["first_in_window_auth_200_seconds"] is None:
                        row["first_in_window_auth_200_seconds"] = round(completed, 3)
                row["samples"].append(sample)
                time.sleep(1)
            check_cancelled()
            final = state(name)
            row.update({key: final[key] for key in ("Running", "OOMKilled", "ExitCode")})
            row["post_window_auth_http_status"] = status(name, password) if final["Running"] else None
            check_cancelled()
            row["post_window_anonymous_http_status"] = status(name) if final["Running"] else None
            check_cancelled()
        finally:
            # docker run can create the container before reporting a start failure.
            if name in docker("ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}").splitlines():
                docker("rm", "-fv", name)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--seconds", type=int, default=25, choices=range(10, 61), metavar="10..60")
    parser.add_argument("--memory-mib", type=int, choices=(128, 384), default=128)
    parser.add_argument("--pairs", type=int, default=2, choices=range(1, 6), metavar="1..5")
    args = parser.parse_args()
    # Context name alone does not exclude a user-defined remote endpoint.
    endpoint = command("docker", "context", "inspect", "desktop-linux", "--format", "{{.Endpoints.docker.Host}}")
    if endpoint != f"unix://{Path.home()}/.docker/run/docker.sock" or not Path(endpoint[7:]).is_socket():
        parser.error("requires the standard local Docker Desktop Unix socket")
    info = json.loads(docker("info", "--format", "{{json .}}"))
    if info["OperatingSystem"] != "Docker Desktop" or info["OSType"] != "linux" or info["CgroupVersion"] != "2":
        parser.error("requires Docker Desktop Linux with cgroup v2")
    for image in (BASELINE, CANDIDATE):
        if docker("image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image) != "linux/arm64":
            parser.error("requires locally cached pinned linux/arm64 images")
    if info["Architecture"] != "aarch64":
        parser.error("requires ARM64 Docker Desktop for native execution")
    memory_limit = args.memory_mib * 1024 * 1024
    result = {
        "architecture": info["Architecture"], "cgroup_version": info["CgroupVersion"],
        "docker_server_version": info["ServerVersion"], "docker_vm_memory_bytes": info["MemTotal"],
        "memory_max_bytes": memory_limit, "memory_swap_max_bytes": 0, "cpus": 0.5,
        "observation_seconds": args.seconds, "trials": [],
    }
    stop_signal = None

    def request_stop(signum, _frame):
        # Let the bounded Docker command return before checking for its container.
        nonlocal stop_signal
        stop_signal = signum

    def check_cancelled():
        if stop_signal is not None:
            raise SystemExit(128 + stop_signal)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    with args.report.open("x") as report:
        try:
            for _ in range(args.pairs):
                for image in (BASELINE, CANDIDATE):
                    result["trials"].append(probe(image, args.seconds, memory_limit, check_cancelled))
            check_cancelled()
        except BaseException as exc:
            result["incomplete"] = signal.Signals(stop_signal).name if stop_signal is not None else type(exc).__name__
            raise
        finally:
            json.dump(result, report, indent=2)
            report.write("\n")


if __name__ == "__main__":
    main()
