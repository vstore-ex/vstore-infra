"""Use the production Redis command/limits and unchanged application snapshots."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/redis-policy"
PROJECT = "redis-policy-" + uuid.uuid4().hex[:12]
IMAGE = PROJECT + ":probe"
REPORT = {"project": PROJECT, "production_modified": False}


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def compose(*args):
    return run("docker", "compose", *args, cwd=directory, env=environment)


def probe(operation):
    return json.loads(compose("exec", "-T", "probe", "python", "probe.py", operation))


def redis_cli(*args):
    return compose("exec", "-T", "redis", "redis-cli", "--raw", *args)


def info(section):
    return dict(line.split(":", 1) for line in redis_cli("INFO", section).splitlines()
                if ":" in line and not line.startswith("#"))


def wait_ready():
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            state = info("persistence")
            if redis_cli("PING") == "PONG" and state["aof_rewrite_in_progress"] == "0":
                assert state["aof_last_write_status"] == state["aof_last_bgrewrite_status"] == "ok"
                return
        except subprocess.CalledProcessError:
            pass
        time.sleep(0.2)
    raise AssertionError("Redis did not become ready")


def helper(command, success=True):
    result = subprocess.run(["python3", str(ROOT / "scripts/redis-policy.py"), command],
                            cwd=directory, env=environment, text=True, capture_output=True)
    assert (result.returncode == 0) == success, result.stderr
    return result


for name, source in json.loads((FIXTURE / "sources.json").read_text()).items():
    assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == source["sha256"]
production = json.loads(run("docker", "compose", "--env-file", ".env.example", "config",
                            "--format", "json", cwd=ROOT))
redis_service = production["services"]["redis"]
assert redis_service["deploy"]["resources"]["limits"]["memory"] == "268435456"
assert redis_service["ports"][0]["host_ip"] == "127.0.0.1"

with tempfile.TemporaryDirectory(prefix="vstore-redis-policy-") as temporary:
    directory = Path(temporary)
    environment = {**os.environ, "COMPOSE_PROJECT_NAME": PROJECT,
                   "BUILDX_CONFIG": str(directory / "buildx")}
    fixture_service = {key: redis_service[key] for key in
                       ("image", "command", "labels", "deploy", "logging")}
    fixture_service.update(volumes=["data:/data"], restart="no",
                           healthcheck={"test": ["CMD", "redis-cli", "ping"],
                                        "interval": "1s", "timeout": "2s", "retries": 30})
    fixture = {"services": {"redis": fixture_service,
                            "probe": {"image": IMAGE, "build": {"context": str(FIXTURE)}}},
               "volumes": {"data": {}}}
    (directory / "docker-compose.yml").write_text(json.dumps(fixture))
    (directory / ".env").write_text("# disposable fixture; no secrets\n")
    legacy = directory / "legacy.json"
    legacy.write_text(json.dumps({"services": {"redis": {
        "command": ["redis-server", "--save", "", "--appendonly", "no"]}}}))
    try:
        compose("build", "probe")
        compose("-f", "docker-compose.yml", "-f", str(legacy), "up", "-d", "--wait")
        assert redis_cli("CONFIG", "GET", "appendonly").splitlines()[-1] == "no"
        REPORT["baseline"] = probe("seed")
        # The real URL factory currently overrides all requested DB numbers with /0.
        assert set(REPORT["baseline"]["dbs"].values()) == {0}
        before = compose("ps", "-q", "redis")
        helper("check", success=False)
        assert compose("ps", "-q", "redis") == before
        assert redis_cli("LLEN", "default") == "1"
        REPORT["preflight"] = "rejects RDB-only live instance without recreation"

        redis_cli("SAVE")
        # Raw recreation is guarded too: mount the snapshot in a separate volume.
        guard_volume = PROJECT + "-guard"
        run("docker", "volume", "create", guard_volume)
        run("docker", "run", "--rm", "--volumes-from", before,
            "-v", guard_volume + ":/guard", redis_service["image"],
            "sh", "-ec", "cp /data/dump.rdb /guard/dump.rdb")
        guard = PROJECT + "-guard"
        try:
            guard_result = subprocess.run(["docker", "run", "--name", guard,
                "-v", guard_volume + ":/data", redis_service["image"],
                *redis_service["command"]], text=True, capture_output=True)
            assert guard_result.returncode != 0 and "RDB-only Redis" in guard_result.stderr
            REPORT["startup_guard"] = "refuses snapshot-only volume; does not create empty AOF"
        finally:
            subprocess.run(["docker", "rm", "-f", guard], check=False, stdout=subprocess.DEVNULL)
            subprocess.run(["docker", "volume", "rm", guard_volume], check=False, stdout=subprocess.DEVNULL)

        helper("adopt")
        assert compose("ps", "-q", "redis") != before
        wait_ready()
        REPORT["reservation"] = probe("reserve")
        REPORT["adoption"] = probe("verify")
        assert info("memory")["maxmemory"] == str(64 * 1024 * 1024)
        prepared = json.loads(helper("prepare").stdout)
        assert prepared == {"services": {"redis": {"command": redis_service["command"]}}}

        # Redis command stays durable when a pre-A07 Git config is restored.
        pin = directory / "policy.json"
        pin.write_text(json.dumps(prepared))
        compose("-f", "docker-compose.yml", "-f", str(legacy), "-f", str(pin),
                "up", "-d", "--no-deps", "--wait", "redis")
        REPORT["config_rollback"] = probe("verify")

        compose("restart", "redis")
        wait_ready()
        REPORT["graceful_restart"] = probe("verify")
        cid = compose("ps", "-q", "redis")
        run("docker", "kill", "--signal", "KILL", cid)
        compose("up", "-d", "--wait", "redis")
        wait_ready()
        REPORT["sigkill_after_fsync"] = probe("verify")
        REPORT["memory_pressure"] = probe("pressure")
        wait_ready()
        cid = compose("ps", "-q", "redis")
        events = run("docker", "exec", cid, "cat", "/sys/fs/cgroup/memory.events")
        counters = dict(line.split() for line in events.splitlines())
        assert counters["oom_kill"] == "0", events
        peak = int(run("docker", "exec", cid, "cat", "/sys/fs/cgroup/memory.peak"))
        assert peak < 256 * 1024 * 1024
        REPORT["memory_pressure"].update(cgroup_peak_bytes=peak, container_limit_bytes=256 * 1024 * 1024,
                                         oom_kill=0, aof_rewrite="passed with concurrent writes")
        assert run("docker", "inspect", "--format", "{{.State.OOMKilled}}", cid) == "false"
        probe("cleanup")
        REPORT["after_pressure"] = probe("verify")
        REPORT["oom_error_baseline"] = probe("oom")
        assert info("memory")["maxmemory"] == str(64 * 1024 * 1024)
        REPORT["outage_baseline"] = probe("outage")
        REPORT["redelivery"] = probe("redeliver")

        # A new empty volume represents lost state without deleting production data.
        compose("down", "--volumes")
        compose("up", "-d", "--wait")
        REPORT["lost_volume_baseline"] = probe("lost")
        # Incomplete AOF input must stop startup rather than silently drop state.
        redis_cli("SET", "a07-corruption-fixture", "synthetic")
        cid = compose("ps", "-q", "redis")
        volume = json.loads(run("docker", "inspect", "--format", "{{json .Mounts}}", cid))[0]["Name"]
        compose("stop", "redis")
        run("docker", "run", "--rm", "-v", volume + ":/data", redis_service["image"],
            "sh", "-ec", "file=$(awk '$NF == \"i\" {print $2}' /data/appendonlydir/appendonly.aof.manifest); "
            "test -n \"$file\"; printf '*2\\r\\n$3\\r\\nSET\\r\\n' >> /data/appendonlydir/\"$file\"")
        compose("start", "redis")
        deadline = time.monotonic() + 15
        while run("docker", "inspect", "--format", "{{.State.Running}}", cid) == "true":
            assert time.monotonic() < deadline, "Truncated AOF did not fail startup"
            time.sleep(0.1)
        assert run("docker", "inspect", "--format", "{{.State.ExitCode}}", cid) != "0"
        assert "aof-load-truncated" in run("docker", "logs", cid)
        REPORT["truncated_aof"] = "startup fails; no automatic truncation or repair"
        print("PASS: adoption, graceful restart, SIGKILL, AOF rewrite and bounded noeviction pressure", flush=True)
        print("PASS: unchanged backend loss/error paths characterized; owner acceptance remains required", flush=True)
        if os.getenv("REDIS_POLICY_REPORT"):
            Path(os.environ["REDIS_POLICY_REPORT"]).write_text(json.dumps(REPORT, indent=2) + "\n")
    finally:
        subprocess.run(["docker", "compose", "down", "--volumes", "--remove-orphans"],
                       cwd=directory, env=environment, check=False)
        subprocess.run(["docker", "image", "rm", IMAGE], check=False, stdout=subprocess.DEVNULL)
