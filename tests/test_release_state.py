import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release-state.py"
spec = importlib.util.spec_from_file_location("release_state", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def image(letter, repository="ghcr.io/vstore-ex/test"):
    return f"{repository}@sha256:{letter * 64}"


class ReleaseStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.old_directory = Path.cwd()
        os.chdir(self.directory.name)
        self.current = dict(zip(release.GROUPS, (image("a"), image("b"), image("c"))))
        self.previous = dict.fromkeys(release.GROUPS, "")

    def tearDown(self):
        os.chdir(self.old_directory)
        self.directory.cleanup()

    def save(self):
        release.save(self.current, self.previous)
        return release.STATE.read_bytes()

    def test_new_process_reads_all_persisted_images(self):
        self.save()
        result = subprocess.check_output(["python3", str(SCRIPT), "env"], text=True)
        self.assertEqual(dict(line.split("=", 1) for line in result.splitlines()), self.current)

    def test_success_updates_only_selected_group_and_previous(self):
        self.save()
        with patch.object(release, "verify") as verify:
            release.commit("FRONTEND_IMAGE", image("d"))
        expected = {**self.current, "FRONTEND_IMAGE": image("d")}
        verify.assert_called_once_with(expected)
        self.assertEqual(release.load(), (expected, {**self.previous, "FRONTEND_IMAGE": image("b")}))

    def test_repeated_commit_keeps_previous_successful_image(self):
        self.previous["BACKEND_IMAGE"] = image("e")
        self.save()
        with patch.object(release, "verify"):
            release.commit("BACKEND_IMAGE", self.current["BACKEND_IMAGE"])
        self.assertEqual(release.load()[1], self.previous)

    def test_failed_verification_never_publishes_candidate(self):
        before = self.save()
        with patch.object(release, "verify", side_effect=ValueError("unhealthy")):
            with self.assertRaises(ValueError):
                release.commit("FRONTEND_IMAGE", image("d"))
        self.assertEqual(release.STATE.read_bytes(), before)

    def test_interruption_before_replace_preserves_whole_release(self):
        before = self.save()
        with patch.object(release.os, "replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                release.save({**self.current, "FRONTEND_IMAGE": image("d")}, self.previous)
        self.assertEqual(release.STATE.read_bytes(), before)
        self.assertEqual(list(Path(".").glob(".release-state-*")), [])

    def test_write_failure_preserves_whole_release(self):
        before = self.save()
        with patch.object(release.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                release.save({**self.current, "FRONTEND_IMAGE": image("d")}, self.previous)
        self.assertEqual(release.STATE.read_bytes(), before)

    def test_release_file_is_private(self):
        self.save()
        self.assertEqual(release.STATE.stat().st_mode & 0o777, 0o600)

    def test_worker_must_share_backend_digest(self):
        self.save()
        data = json.loads(release.STATE.read_text())
        data["services"]["worker"]["image"] = image("d")
        release.STATE.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            release.load()

    def test_mutable_or_shell_image_references_are_rejected(self):
        for ref in ("nginx:latest", "$(touch leaked)", "nginx@sha256:123", "nginx@sha256:" + "A" * 64):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                release.immutable(ref)
        self.assertFalse(Path("leaked").exists())

    def test_bootstrap_checks_every_running_service_before_writing(self):
        calls = []

        def running(service, expected=None):
            calls.append((service, expected))
            return expected or self.current[next(key for key, services in release.GROUPS.items() if service in services)]

        with patch.object(release, "running_image", side_effect=running):
            release.bootstrap()
        self.assertEqual(release.load(), (self.current, self.previous))
        self.assertEqual([service for service, _ in calls], ["backend", "worker", "frontend", "admin"])
        self.assertEqual(calls[1][1], self.current["BACKEND_IMAGE"])

    def test_failed_adoption_creates_no_state(self):
        with patch.object(release, "running_image", side_effect=ValueError("no registry digest")):
            with self.assertRaises(ValueError):
                release.bootstrap()
        self.assertFalse(release.STATE.exists())

    def test_bootstrap_never_overwrites_existing_release(self):
        before = self.save()
        with self.assertRaises(ValueError):
            release.bootstrap()
        release.bootstrap(if_missing=True)
        self.assertEqual(release.STATE.read_bytes(), before)

    def test_bootstrap_uses_two_step_image_inspection(self):
        calls = []

        def docker(*args):
            calls.append(args)
            if args[:2] == ("compose", "ps"):
                return "container-id"
            if "{{json .State}}" in args:
                return json.dumps({"Running": True, "Health": {"Status": "healthy"}})
            if "{{.Image}}" in args:
                return "local-image-id"
            if "{{json .RepoDigests}}" in args:
                self.assertEqual(args[-1], "local-image-id")
                return json.dumps([self.current["BACKEND_IMAGE"]])
            if "{{.Config.Image}}" in args:
                return "ghcr.io/vstore-ex/test:latest"
            self.fail(str(args))

        with patch.object(release, "docker", side_effect=docker):
            self.assertEqual(release.running_image("backend"), self.current["BACKEND_IMAGE"])
        self.assertTrue(any("{{.Image}}" in args for args in calls))

    def test_unhealthy_or_wrong_image_never_verifies(self):
        for state in ({"Running": False}, {"Running": True, "Health": {"Status": "unhealthy"}}):
            with patch.object(release, "docker", side_effect=["container", json.dumps(state)]):
                with self.assertRaises(ValueError):
                    release.running_image("frontend", image("d"))
        with patch.object(release, "docker", side_effect=[
            "container", json.dumps({"Running": True, "Health": {"Status": "healthy"}}),
            "image-id", json.dumps([image("b")]),
        ]):
            with self.assertRaises(ValueError):
                release.running_image("frontend", image("d"))

    def test_preflight_allows_repairing_unhealthy_verified_image(self):
        with patch.object(release, "docker", side_effect=[
            "container", json.dumps({"Running": True, "Health": {"Status": "unhealthy"}}),
            "image-id", json.dumps([image("b")]), "image-id",
        ]):
            self.assertEqual(release.running_image("frontend", image("b"), healthy=False), image("b"))

    def test_intentional_rollback_persists_target_for_next_session(self):
        self.previous["FRONTEND_IMAGE"] = image("d")
        self.save()
        with patch.object(release, "candidate_compose"), patch.object(release, "check_http"), patch.object(release, "verify"):
            release.rollback("FRONTEND_IMAGE")
        current, previous = release.load()
        self.assertEqual(current["FRONTEND_IMAGE"], image("d"))
        self.assertEqual(previous["FRONTEND_IMAGE"], image("b"))
        result = subprocess.check_output(["python3", str(SCRIPT), "env"], text=True)
        self.assertIn("FRONTEND_IMAGE=" + image("d"), result)

    def test_failed_manual_rollback_restores_current_without_publishing(self):
        self.previous["FRONTEND_IMAGE"] = image("d")
        before = self.save()
        with patch.object(release, "candidate_compose") as compose, patch.object(release, "check_http", side_effect=[ValueError("HTTP failed"), None]):
            with self.assertRaises(ValueError):
                release.rollback("FRONTEND_IMAGE")
        self.assertEqual(release.STATE.read_bytes(), before)
        self.assertEqual(compose.call_args.args[0], self.current)

    def test_missing_rollback_target_never_changes_containers(self):
        self.save()
        with patch.object(release, "candidate_compose") as compose, self.assertRaises(ValueError):
            release.rollback("ADMIN_IMAGE")
        compose.assert_not_called()

    def test_inherited_lock_is_same_shared_lock(self):
        with open(".deploy.lock", "a") as stream:
            release.fcntl.flock(stream, release.fcntl.LOCK_EX)
            with release.locked(stream.fileno()):
                self.save()
        with tempfile.TemporaryFile() as other, self.assertRaises(ValueError):
            with release.locked(other.fileno()):
                pass

    def test_compose_keeps_lock_until_command_exits(self):
        self.save()
        process = subprocess.Popen([
            "python3", "-c",
            "import fcntl,time; f=open('.deploy.lock','a'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(.3)",
        ], stdout=subprocess.PIPE, text=True)
        self.assertEqual(process.stdout.readline().strip(), "locked")
        result = subprocess.run(["python3", str(SCRIPT), "env"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(process.poll())
        process.wait()
        process.stdout.close()

    def test_log_follower_does_not_hold_deployment_lock(self):
        self.save()
        with patch.object(release.sys, "argv", [str(SCRIPT), "compose", "logs", "-f", "backend"]), \
                patch.object(release, "locked", side_effect=ValueError("read-only command acquired lock")), \
                patch.object(release.subprocess, "call", return_value=0) as compose:
            with self.assertRaises(SystemExit) as result:
                release.main()
        self.assertEqual(result.exception.code, 0)
        compose.assert_called_once_with(["docker", "compose", "logs", "-f", "backend"])


if __name__ == "__main__":
    unittest.main()
