import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = "scripts/monitoring-deploy.sh"
UNITS = ("vstore-monitoring.service", "vstore-monitoring-watchdog.service",
         "vstore-monitoring-watchdog.timer")


def stub(path, body):
    path.write_text("#!/usr/bin/env bash\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Deploy:
    """A deployment checkout with two commits and recording host stubs."""

    def __init__(self, temp, *, enabled=True, restart_rc=0, change="monitoring/prometheus.yml"):
        self.repo = Path(temp) / "deploy"
        self.repo.mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True,
                                        capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        (self.repo / "scripts").mkdir()
        (self.repo / "monitoring/systemd").mkdir(parents=True)
        shutil.copy(ROOT / SCRIPT, self.repo / SCRIPT)
        for name in UNITS:
            (self.repo / "monitoring/systemd" / name).write_text(f"[Unit]\nDescription={name}\n")
        (self.repo / "monitoring/prometheus.yml").write_text("a\n")
        (self.repo / "README.md").write_text("a\n")
        git("add", "-A")
        git("commit", "-qm", "one")
        self.prev = git("rev-parse", "HEAD")
        # Units installed on the host come from the previous deployment.
        self.installed = Path(temp) / "systemd"
        self.installed.mkdir()
        for name in UNITS:
            shutil.copy(self.repo / "monitoring/systemd" / name, self.installed / name)
        target = self.repo / change
        target.write_text(target.read_text() + "b\n")
        git("commit", "-qam", "two")
        self.new = git("rev-parse", "HEAD")
        self.log = Path(temp) / "calls.log"
        self.bin = Path(temp) / "bin"
        self.bin.mkdir()
        enabled_rc = 0 if enabled else 1
        stub(self.bin / "systemctl",
             f'echo "systemctl $*" >> {self.log}; '
             f'case "$1" in is-enabled) exit {enabled_rc};; try-restart) exit {restart_rc};; esac; exit 0')
        stub(self.bin / "sudo", f'echo "sudo $*" >> {self.log}; [ "$1" = -n ] && shift; exec "$@"')

    def run(self, prev=None):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "MONITORING_SYSTEMD_DIR": str(self.installed),
               "HOME": str(self.repo.parent)}
        return subprocess.run(
            ["bash", SCRIPT, self.prev if prev is None else prev, self.new],
            cwd=self.repo, env=env, capture_output=True, text=True, check=False,
        )

    def restarted(self):
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return "sudo -n systemctl try-restart vstore-monitoring.service" in calls


class MonitoringDeployTests(unittest.TestCase):
    def test_changed_monitoring_config_restarts_an_active_service(self):
        with tempfile.TemporaryDirectory() as temp:
            deploy = Deploy(temp)
            result = deploy.run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(deploy.restarted(), result.stdout)

    def test_unrelated_change_leaves_monitoring_running_as_is(self):
        with tempfile.TemporaryDirectory() as temp:
            deploy = Deploy(temp, change="README.md")
            result = deploy.run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(deploy.restarted())

    def test_host_without_the_service_is_left_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            deploy = Deploy(temp, enabled=False)
            result = deploy.run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(deploy.restarted())

    def test_restart_failure_warns_without_failing_the_deployment(self):
        with tempfile.TemporaryDirectory() as temp:
            deploy = Deploy(temp, restart_rc=1)
            result = deploy.run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("::warning::", result.stdout)

    def test_first_deploy_without_previous_commit_restarts(self):
        with tempfile.TemporaryDirectory() as temp:
            deploy = Deploy(temp, change="README.md")
            result = deploy.run(prev="")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(deploy.restarted())

    def test_outdated_installed_units_are_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            deploy = Deploy(temp, change="monitoring/systemd/vstore-monitoring.service")
            result = deploy.run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("::warning::", result.stdout)
            self.assertIn("vstore-monitoring.service", result.stdout)
            self.assertIn("install-service", result.stdout)

    def test_workflow_runs_hook_after_successful_application_deploy(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        applied = workflow.index("Infra stack config applied successfully")
        hook = workflow.index(f'bash {SCRIPT} "$PREV_GIT_COMMIT" "$DEPLOY_SHA"')
        self.assertGreater(hook, applied)
        line = workflow[hook:workflow.index("\n", hook)]
        self.assertIn("|| echo", line, "the hook must never fail the deployment")


if __name__ == "__main__":
    unittest.main()
