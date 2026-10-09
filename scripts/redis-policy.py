"""Check Redis adoption before recreation; convert only with stopped writers."""

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MAX_MEMORY = 64 * 1024 * 1024
ADOPTION_MEMORY = 48 * 1024 * 1024
POLICY = {
    "appendonly": "yes", "appendfsync": "everysec",
    "no-appendfsync-on-rewrite": "no", "aof-load-truncated": "no",
    "save": "", "maxmemory": str(MAX_MEMORY), "maxmemory-policy": "noeviction",
    "auto-aof-rewrite-percentage": "100", "auto-aof-rewrite-min-size": str(MAX_MEMORY),
}


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def compose(*args):
    return run("docker", "compose", *args)


def desired():
    # Capture, never print, resolved Compose secrets. Return only Redis's command.
    service = json.loads(compose("config", "--format", "json"))["services"].get("redis", {})
    if service.get("labels", {}).get("vstore.redis.policy") != "a07-v1":
        return None
    return service["command"]


def container():
    cid = compose("ps", "-q", "redis")
    if not cid or "\n" in cid:
        raise ValueError("Expected one running Redis; inspect its data volume before starting it")
    return cid


def cli(cid, *args):
    # redis-cli otherwise reports Redis errors with exit status zero.
    value = run("docker", "exec", cid, "redis-cli", "--raw", *args)
    if value.startswith(("ERR ", "OOM ", "MISCONF ")):
        raise ValueError("Redis rejected the policy operation")
    return value


def info(cid, section):
    return dict(line.split(":", 1) for line in cli(cid, "INFO", section).splitlines()
                if ":" in line and not line.startswith("#"))


def durable(state):
    return (state.get("aof_enabled") == "1"
            and state.get("aof_rewrite_in_progress") == "0"
            and state.get("aof_rewrite_scheduled") == "0"
            and state.get("aof_last_bgrewrite_status") == "ok"
            and state.get("aof_last_write_status") == "ok"
            and state.get("aof_pending_bio_fsync", "0") == "0")


def check():
    command = desired()
    if command is None:
        return
    try:
        cid = container()
    except ValueError:
        # A previously adopted stopped container already has durable startup flags.
        # Redis itself checks its AOF when starting; never admit a legacy command.
        cid = compose("ps", "-a", "-q", "redis")
        if cid and "\n" not in cid:
            actual = json.loads(run("docker", "inspect", "--format", "{{json .Config.Cmd}}", cid))
            if actual == command:
                return
        raise ValueError("Stopped Redis has no verified AOF startup command; inspect its volume before starting it")
    if info(cid, "persistence").get("aof_enabled") != "1":
        raise ValueError("Live Redis AOF is not ready; stop writers and run reviewed redis-adopt first")
    wait_durable(cid)
    run("docker", "exec", cid, "test", "-s", "/data/appendonlydir/appendonly.aof.manifest")
    if int(info(cid, "memory")["used_memory"]) > ADOPTION_MEMORY:
        raise ValueError("Redis exceeds the 48 MiB adoption budget; review capacity before recreation")


def wait_durable(cid, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if durable(info(cid, "persistence")):
            return
        time.sleep(0.2)
    raise TimeoutError("Redis AOF conversion did not complete; retain AOF and keep writers stopped")


def adopt():
    if desired() is None:
        raise ValueError("Install the reviewed A07 Compose policy before adoption")
    if {"backend", "worker"} & set(compose("ps", "--status", "running", "--services").splitlines()):
        raise ValueError("Stop backend and worker, including external writers, before Redis adoption")
    cid = container()
    if int(info(cid, "memory")["used_memory"]) > ADOPTION_MEMORY:
        raise ValueError("Redis exceeds the 48 MiB adoption budget; review capacity first")
    # Require free space for the old dataset and rewrite plus a conservative margin.
    run("docker", "exec", cid, "sh", "-ec",
        "test $(df -Pk /data | awk 'NR==2 {print $4}') -ge 262144")
    cli(cid, "SAVE")  # recoverable snapshot of the quiescent live dataset
    for key, value in POLICY.items():
        cli(cid, "CONFIG", "SET", key, value)
    wait_durable(cid)
    check()
    # Runtime CONFIG SET alone disappears on restart. Persist flags in Compose now.
    compose("up", "-d", "--no-deps", "--pull", "never", "--wait", "--wait-timeout", "90", "redis")
    check()
    cid = container()
    for key, expected in POLICY.items():
        values = run("docker", "exec", cid, "redis-cli", "--json", "CONFIG", "GET", key)
        if json.loads(values) != {key: expected}:
            raise ValueError("Redis did not retain the reviewed policy after recreation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-fd", type=int)
    parser.add_argument("command", choices=("check", "prepare", "adopt"))
    args = parser.parse_args()
    # Protect the lock before opening it; share the same release/deployment lock.
    subprocess.run([sys.executable, str(ROOT / "scripts/secure-deployment.py"), "--lock-only"],
                   stdout=subprocess.DEVNULL, check=True)
    spec = importlib.util.spec_from_file_location("release_state", ROOT / "scripts/release-state.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    with release.locked(args.lock_fd):
        if args.command == "adopt":
            adopt()
            print("Redis AOF adoption and policy verification passed; verify app recovery before resuming traffic")
        else:
            check()
            if args.command == "prepare":
                command = desired()
                print(json.dumps({"services": {"redis": {"command": command}} if command else {}}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, TimeoutError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
