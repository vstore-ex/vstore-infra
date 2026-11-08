import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "monitoring/systemd"
SCRIPT = "scripts/monitoring-service.sh"


def unit(name):
    sections, current = {}, None
    for line in (UNITS / name).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), {})
            continue
        key, value = line.split("=", 1)
        current.setdefault(key, []).append(value)
    return sections


class Host:
    """A temporary deployment checkout with recording stubs for host commands."""

    def __init__(self, temp, *, health="ok", guard_rc=0, active=True):
        self.root = Path(temp) / "deploy"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "monitoring").mkdir()
        shutil.copy(ROOT / SCRIPT, self.root / SCRIPT)
        self.log = Path(temp) / "calls.log"
        self.bin = Path(temp) / "bin"
        self.bin.mkdir()
        self.state = Path(temp) / "state"
        self.slice = Path(temp) / "slice"
        self.slice.mkdir()
        self.psi = Path(temp) / "io"
        self.env_file = Path(temp) / "private/compose.env"
        self.env_file.parent.mkdir()
        self.env_file.write_text("MONITORING_SECRETS_DIR=x\n")
        self.write_stub(self.root / "scripts/monitoring-guard.sh", f'echo "guard $*" >> {self.log}; exit {guard_rc}')
        self.write_stub(self.bin / "docker", f'echo "docker $*" >> {self.log}')
        curl = f'echo "curl $*" >> {self.log}; ' + ("exit 0" if health == "ok" else "exit 7")
        self.write_stub(self.bin / "curl", curl)
        rc = 0 if active else 3
        self.write_stub(
            self.bin / "systemctl",
            f'echo "systemctl $*" >> {self.log}; [ "$1" = is-active ] && exit {rc}; exit 0',
        )
        self.set_pressure(0.0)
        self.set_oom(0)

    @staticmethod
    def write_stub(path, body):
        path.write_text("#!/usr/bin/env bash\n" + body + "\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def set_pressure(self, avg300):
        self.psi.write_text(
            f"some avg10=0.00 avg60=0.00 avg300={avg300:.2f} total=1\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=1\n"
        )

    def set_oom(self, count):
        (self.slice / "memory.events").write_text(
            f"low 0\nhigh 0\nmax 0\noom 0\noom_kill {count}\noom_group_kill 0\n"
        )

    def run(self, *args):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "VSTORE_DEPLOY_PATH": str(self.root),
            "MONITORING_ENV_FILE": str(self.env_file),
            "MONITORING_HEALTH_TIMEOUT": "2",
            "MONITORING_PSI_IO": str(self.psi),
            "MONITORING_SLICE_DIR": str(self.slice),
            "MONITORING_STATE_DIR": str(self.state),
        }
        return subprocess.run(
            ["bash", str(self.root / SCRIPT), *args],
            env=env, capture_output=True, text=True, check=False,
        )

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


class ServiceUnitTests(unittest.TestCase):
    def test_service_starts_from_deployment_checkout_after_docker_and_slice(self):
        service = unit("vstore-monitoring.service")
        for dependency in ("docker.service", "vstore-monitoring.slice"):
            self.assertIn(dependency, " ".join(service["Unit"]["Requires"]))
            self.assertIn(dependency, " ".join(service["Unit"]["After"]))
        self.assertEqual(service["Service"]["EnvironmentFile"], ["/etc/vstore-monitoring/service.env"])
        self.assertEqual(
            service["Service"]["ExecStart"],
            ["/bin/bash ${VSTORE_DEPLOY_PATH}/scripts/monitoring-service.sh start"],
        )
        self.assertEqual(
            service["Service"]["ExecStop"],
            ["/bin/bash ${VSTORE_DEPLOY_PATH}/scripts/monitoring-service.sh stop"],
        )
        self.assertEqual(service["Install"]["WantedBy"], ["multi-user.target"])

    def test_watchdog_timer_runs_every_minute_after_boot(self):
        timer = unit("vstore-monitoring-watchdog.timer")
        self.assertEqual(timer["Timer"]["Unit"], ["vstore-monitoring-watchdog.service"])
        self.assertEqual(timer["Timer"]["OnUnitActiveSec"], ["1min"])
        self.assertEqual(timer["Install"]["WantedBy"], ["timers.target"])
        watchdog = unit("vstore-monitoring-watchdog.service")
        self.assertEqual(
            watchdog["Service"]["ExecStart"],
            ["/bin/bash ${VSTORE_DEPLOY_PATH}/scripts/monitoring-service.sh watchdog"],
        )

    def test_monitoring_data_is_never_deleted_by_the_service(self):
        script = (ROOT / SCRIPT).read_text()
        self.assertNotRegex(script, r"\bdown\b")
        self.assertNotIn("-v", re.findall(r"compose[^\n]*", script)[0].split())


class ServiceStartStopTests(unittest.TestCase):
    def test_start_waits_for_application_then_guard_then_compose(self):
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp)
            result = host.run("start")
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = host.calls()
            order = [next(i for i, c in enumerate(calls) if c.startswith(p))
                     for p in ("curl", "guard check", "docker compose")]
            self.assertEqual(order, sorted(order), calls)
            compose = next(c for c in calls if c.startswith("docker compose"))
            self.assertIn(f"-p vstore-monitoring --env-file {host.env_file}", compose)
            self.assertIn(f"-f {host.root}/monitoring/compose.yml -f {host.root}/monitoring/production.yml", compose)
            self.assertTrue(compose.endswith("up -d --wait"), compose)

    def test_start_fails_closed_without_application_health(self):
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp, health="down")
            result = host.run("start")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(any(c.startswith(("guard", "docker")) for c in host.calls()))

    def test_start_fails_closed_when_guard_rejects_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp, guard_rc=1)
            result = host.run("start")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(any(c.startswith("docker") for c in host.calls()))

    def test_stop_keeps_containers_and_volumes(self):
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp)
            result = host.run("stop")
            self.assertEqual(result.returncode, 0, result.stderr)
            compose = [c for c in host.calls() if c.startswith("docker compose")]
            self.assertEqual(len(compose), 1)
            self.assertTrue(compose[0].endswith(" stop"), compose)


class WatchdogTests(unittest.TestCase):
    def stops(self, host):
        return any(c == "systemctl stop vstore-monitoring.service" for c in host.calls())

    def test_sustained_io_pressure_stops_monitoring_with_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp)
            host.run("start")
            host.set_pressure(25.0)
            result = host.run("watchdog")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self.stops(host))
            self.assertIn("io", (host.state / "last-stop").read_text())

    def test_pressure_below_limit_keeps_monitoring(self):
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp)
            host.run("start")
            host.set_pressure(19.9)
            host.run("watchdog")
            self.assertFalse(self.stops(host))

    def test_new_oom_kill_since_start_stops_monitoring(self):
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp)
            host.set_oom(2)  # earlier kills, before this start
            host.run("start")
            host.run("watchdog")
            self.assertFalse(self.stops(host))
            host.set_oom(3)
            host.run("watchdog")
            self.assertTrue(self.stops(host))
            self.assertIn("oom", (host.state / "last-stop").read_text())

    def test_inactive_service_is_left_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp, active=False)
            host.set_pressure(90.0)
            result = host.run("watchdog")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(self.stops(host))

    def test_readme_documents_the_watchdog_threshold(self):
        script = (ROOT / SCRIPT).read_text()
        limit = re.search(r"^io_limit=(\d+)$", script, re.M)
        self.assertIsNotNone(limit)
        readme = (ROOT / "monitoring/README.md").read_text()
        self.assertIn(f"`some avg300` above {limit.group(1)}%", readme)


class ServiceIdentityTests(unittest.TestCase):
    """2026-11-08 production: root had no `docker compose` (per-user plugin), so the
    service failed to start. It must run as the private configuration's owner."""

    def test_service_keeps_runtime_state_for_an_unprivileged_user(self):
        service = unit("vstore-monitoring.service")["Service"]
        self.assertNotIn("User", service, "the owner is set by the install-service drop-in")
        self.assertEqual(service["RuntimeDirectory"], ["vstore-monitoring"])
        self.assertEqual(service["RuntimeDirectoryPreserve"], ["yes"])

    def test_watchdog_stays_root_to_stop_the_service(self):
        self.assertNotIn("User", unit("vstore-monitoring-watchdog.service")["Service"])

    def test_installer_runs_the_service_as_the_config_owner_in_the_docker_group(self):
        guard = (ROOT / "scripts/monitoring-guard.sh").read_text()
        self.assertIn("/etc/systemd/system/vstore-monitoring.service.d/owner.conf", guard)
        self.assertRegex(guard, r"stat -c '%U' \"\$private_env\"")
        self.assertIn("id -nG", guard)
        self.assertIn("User=%s", guard)


class InstallerTests(unittest.TestCase):
    def test_guard_installs_every_monitoring_unit_from_the_repository(self):
        guard = (ROOT / "scripts/monitoring-guard.sh").read_text()
        self.assertIn("install-service", guard)
        for name in ("vstore-monitoring.service", "vstore-monitoring-watchdog.service",
                     "vstore-monitoring-watchdog.timer"):
            self.assertTrue((UNITS / name).exists(), name)
            self.assertIn(name, guard)


class DatastoreSecretBackfillTests(unittest.TestCase):
    """Private directories prepared before #74 have no postgres-dsn secret."""

    def host(self, temp):
        host = Host(temp)
        private = host.env_file.parent
        host.env_file.write_text(f'MONITORING_SECRETS_DIR="{private}"\n')
        return host, private / "postgres-dsn"

    def test_start_adds_the_placeholder_dsn_before_compose(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("monitoring", ROOT / "scripts/monitoring.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            host, dsn = self.host(temp)
            result = host.run("start")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(dsn.read_text(), module.POSTGRES_PLACEHOLDER)
            self.assertEqual(stat.S_IMODE(dsn.stat().st_mode), 0o600)

    def test_start_keeps_an_existing_dsn(self):
        with tempfile.TemporaryDirectory() as temp:
            host, dsn = self.host(temp)
            dsn.write_text("postgresql://vstore_monitor:kept@127.0.0.1:5432/app")
            result = host.run("start")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(":kept@", dsn.read_text())


if __name__ == "__main__":
    unittest.main()
