import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("redis_policy", ROOT / "scripts/redis-policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)

READY = {"aof_enabled": "1", "aof_rewrite_in_progress": "0", "aof_rewrite_scheduled": "0",
         "aof_last_bgrewrite_status": "ok", "aof_last_write_status": "ok",
         "aof_pending_bio_fsync": "0"}


class RedisPolicyTests(unittest.TestCase):
    def test_incomplete_or_failed_aof_is_not_adopted(self):
        self.assertTrue(policy.durable(READY))
        for key, value in (("aof_enabled", "0"), ("aof_rewrite_in_progress", "1"),
                           ("aof_rewrite_scheduled", "1"), ("aof_last_bgrewrite_status", "err"),
                           ("aof_last_write_status", "err"), ("aof_pending_bio_fsync", "1")):
            with self.subTest(key=key):
                self.assertFalse(policy.durable({**READY, key: value}))
        self.assertFalse(policy.durable({}))

    def test_active_writers_prevent_every_mutation(self):
        for services in ("redis\nbackend", "redis\nworker", "redis\nbackend\nworker"):
            with self.subTest(services=services), patch.object(policy, "desired", return_value=["redis-server"]), \
                    patch.object(policy, "compose", return_value=services), patch.object(policy, "cli") as cli:
                with self.assertRaisesRegex(ValueError, "Stop backend and worker"):
                    policy.adopt()
                cli.assert_not_called()

    def test_oversized_live_dataset_prevents_conversion(self):
        with patch.object(policy, "desired", return_value=["redis-server"]), \
                patch.object(policy, "compose", return_value="redis"), \
                patch.object(policy, "container", return_value="cid"), \
                patch.object(policy, "info", return_value={"used_memory": str(policy.ADOPTION_MEMORY + 1)}), \
                patch.object(policy, "cli") as cli:
            with self.assertRaisesRegex(ValueError, "48 MiB"):
                policy.adopt()
            cli.assert_not_called()

    def test_error_reply_is_not_treated_as_cli_success(self):
        for error in ("ERR bad option", "OOM command not allowed", "MISCONF disk error"):
            with self.subTest(error=error), patch.object(policy, "run", return_value=error):
                with self.assertRaisesRegex(ValueError, "rejected"):
                    policy.cli("cid", "CONFIG", "SET", "appendonly", "yes")

    def test_failed_conversion_does_not_disable_aof_or_recreate(self):
        with patch.object(policy, "desired", return_value=["redis-server"]), \
                patch.object(policy, "compose", return_value="redis") as compose, \
                patch.object(policy, "container", return_value="cid"), \
                patch.object(policy, "info", return_value={"used_memory": "2000000"}), \
                patch.object(policy, "run"), patch.object(policy, "cli") as cli, \
                patch.object(policy, "wait_durable", side_effect=TimeoutError("conversion failed")):
            with self.assertRaises(TimeoutError):
                policy.adopt()
            self.assertFalse(any(call.args[0] == "up" for call in compose.call_args_list))
            self.assertFalse(any(call.args == ("cid", "CONFIG", "SET", "appendonly", "no")
                                 for call in cli.call_args_list))

    def test_missing_policy_is_not_an_adoption_request(self):
        with patch.object(policy, "desired", return_value=None), patch.object(policy, "cli") as cli:
            with self.assertRaisesRegex(ValueError, "Install the reviewed"):
                policy.adopt()
            cli.assert_not_called()

    def test_readiness_failure_prevents_recreation(self):
        with patch.object(policy, "desired", return_value=["redis-server"]), \
                patch.object(policy, "container", return_value="cid"), \
                patch.object(policy, "info", return_value={**READY, "aof_enabled": "0"}), \
                patch.object(policy, "run") as run:
            with self.assertRaisesRegex(ValueError, "AOF is not ready"):
                policy.check()
            run.assert_not_called()

    def test_workflow_preserves_redis_command_through_git_rollback(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        gate = workflow.index("scripts/redis-policy.py --lock-fd 200 prepare")
        self.assertLess(gate, workflow.index('if ! (docker pull "$EDGE_IMAGE"'))
        self.assertIn('-f "$REDIS_OVERRIDE" -f "$PROXY_OVERRIDE"', workflow)
        rollback = workflow[workflow.index('git reset --hard "$PREV_GIT_COMMIT"'):]
        self.assertIn("edge_compose up", rollback)

    def test_stopped_legacy_container_cannot_be_started_as_adopted(self):
        command = ["redis-server", "--appendonly", "yes"]
        for actual, accepted in ((command, True), (["redis-server"], False)):
            with self.subTest(actual=actual), patch.object(policy, "desired", return_value=command), \
                    patch.object(policy, "container", side_effect=ValueError("stopped")), \
                    patch.object(policy, "compose", return_value="cid"), \
                    patch.object(policy, "run", return_value=json.dumps(actual)):
                if accepted:
                    policy.check()
                else:
                    with self.assertRaisesRegex(ValueError, "Stopped Redis"):
                        policy.check()


if __name__ == "__main__":
    unittest.main()
