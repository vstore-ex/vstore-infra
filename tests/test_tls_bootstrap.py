"""Exercise certificate bootstrap ordering and locking without contacting ACME."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TLSBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        scripts = self.directory / "scripts"
        scripts.mkdir()
        for name in ("init-letsencrypt.sh", "release-state.sh", "release-state.py"):
            shutil.copy(ROOT / "scripts" / name, scripts / name)
        self.images = {key: f"ghcr.io/vstore-ex/{key.lower()}@sha256:{letter * 64}"
                       for key, letter in zip(("BACKEND_IMAGE", "FRONTEND_IMAGE", "ADMIN_IMAGE"), "abc")}
        groups = {"BACKEND_IMAGE": ("backend", "worker"), "FRONTEND_IMAGE": ("frontend",), "ADMIN_IMAGE": ("admin",)}
        (self.directory / "docker-compose.override.yml").write_text(json.dumps({
            "x-vstore-release": {"version": 1, "previous": dict.fromkeys(groups, "")},
            "services": {service: {"image": self.images[key]} for key, services in groups.items() for service in services},
        }))
        (self.directory / ".env").write_text("FRONTEND_DOMAIN=store.example.com\nADMIN_DOMAIN=admin.example.com\nBACKEND_IMAGE=unverified:latest\n")
        binary = self.directory / "bin"
        binary.mkdir()
        shim = "#!" + sys.executable + "\n" + '''
import fcntl, json, os
from pathlib import Path
import sys
root = Path(os.environ["TLS_TEST_ROOT"])
def log(value):
    with (root / "events").open("a") as stream:
        stream.write(value + "\\n")
name = Path(sys.argv[0]).name
args = sys.argv[1:]
if name == "flock":
    log("lock-wait")
    fcntl.flock(int(args[-1]), fcntl.LOCK_EX)
    sys.exit(0)
with (root / ".deploy.lock").open("a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise AssertionError("bootstrap command ran without the deployment lock")
if name == "python3":
    if args[0] == "scripts/release-state.py":
        os.execv(os.environ["TLS_TEST_PYTHON"], [os.environ["TLS_TEST_PYTHON"], *args])
    assert args == ["scripts/proxy-health.py", "recover"], args
    log("gate")
    assert (root / "certificate-issued").exists(), "HTTPS gate ran before certificate issuance"
    if os.environ.get("TLS_TEST_GATE_FAIL"):
        sys.exit(1)
elif name == "docker":
    assert args[:3] == ["compose", "-f", "docker-compose.yml"], args
    expected = json.loads(os.environ["TLS_TEST_IMAGES"])
    assert all(os.environ.get(key) == image for key, image in expected.items()), "persisted digests must override .env"
    if args[3] == "up":
        assert args[4:] == ["-d", "--wait", "--wait-timeout", "150", "proxy"], args
        assert not (root / "certificate-issued").exists()
        log("up")
    elif args[3] == "run":
        assert args[4:9] == ["--rm", "--entrypoint", "certbot", "certbot", "certonly"], args
        assert "store.example.com" in args and "admin.example.com" in args
        log("certbot")
        if os.environ.get("TLS_TEST_ISSUE_FAIL"):
            sys.exit(1)
        (root / "certificate-issued").touch()
    else:
        raise AssertionError(args)
else:
    raise AssertionError(name)
'''
        for name in ("flock", "docker", "python3"):
            executable = binary / name
            executable.write_text(shim)
            executable.chmod(0o755)
        self.environment = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                            "TLS_TEST_ROOT": str(self.directory), "TLS_TEST_PYTHON": sys.executable,
                            "TLS_TEST_IMAGES": json.dumps(self.images)}
        self.command = ["bash", str(scripts / "init-letsencrypt.sh")]

    def run_bootstrap(self, **environment):
        return subprocess.run(self.command, env={**self.environment, **environment},
                              cwd=self.directory, capture_output=True, text=True, timeout=15)

    def events(self):
        path = self.directory / "events"
        return path.read_text().splitlines() if path.exists() else []

    def test_certificate_issuance_runs_before_public_https_gate(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), ["lock-wait", "up", "certbot", "gate"])
        self.assertIn("completed successfully", result.stdout)

    def test_failed_issuance_stops_before_gate(self):
        result = self.run_bootstrap(TLS_TEST_ISSUE_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events(), ["lock-wait", "up", "certbot"])
        self.assertNotIn("completed successfully", result.stdout)

    def test_failed_final_https_gate_is_not_success(self):
        result = self.run_bootstrap(TLS_TEST_GATE_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events(), ["lock-wait", "up", "certbot", "gate"])
        self.assertNotIn("completed successfully", result.stdout)

    def test_bootstrap_waits_for_existing_deployment_lock(self):
        with (self.directory / ".deploy.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = subprocess.Popen(self.command, env=self.environment, cwd=self.directory,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while "lock-wait" not in self.events() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(self.events(), ["lock-wait"])
                self.assertIsNone(process.poll())
                fcntl.flock(lock, fcntl.LOCK_UN)
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertIn("completed successfully", stdout)
                self.assertEqual(self.events(), ["lock-wait", "up", "certbot", "gate"])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
