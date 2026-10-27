import json
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ClientIdentityConfigTests(unittest.TestCase):
    def config(self):
        return json.loads(subprocess.check_output(
            ["docker", "compose", "--env-file", ".env.example", "config", "--format", "json"],
            cwd=ROOT, env={**os.environ, "TRUST_PROXY_HEADERS": "true"}, text=True,
        ))

    def test_backend_trusts_exact_proxy_addresses_and_not_raw_headers(self):
        config = self.config()
        services = config["services"]
        backend = services["backend"]
        command = backend["command"]
        allowed = command[command.index("--forwarded-allow-ips") + 1].split(",")
        expected = [services[name]["networks"]["proxy_net"]["ipv4_address"] for name in ("proxy", "frontend", "admin")]
        self.assertEqual(allowed, expected)
        self.assertNotIn("*", allowed)
        self.assertNotIn(backend["networks"]["proxy_net"]["ipv4_address"], allowed)
        self.assertEqual(backend["environment"]["TRUST_PROXY_HEADERS"], "false")
        self.assertIn("--proxy-headers", command)

    def test_proxy_transport_is_separate_from_data_network(self):
        services = self.config()["services"]
        for name in ("proxy", "frontend", "admin"):
            self.assertEqual(set(services[name]["networks"]), {"proxy_net"})
        self.assertEqual(set(services["backend"]["networks"]), {"proxy_net", "app_net"})
        for name in ("postgres", "redis", "worker"):
            self.assertEqual(set(services[name]["networks"]), {"app_net"})

    def test_app_scheme_trust_matches_custom_edge_address(self):
        config = json.loads(subprocess.check_output(
            ["docker", "compose", "--env-file", ".env.example", "config", "--format", "json"],
            cwd=ROOT, env={**os.environ, "EDGE_PROXY_IP": "172.31.240.8"}, text=True,
        ))
        services = config["services"]
        for name in ("frontend", "admin"):
            self.assertEqual(services[name]["environment"]["EDGE_PROXY_IP"],
                             services["proxy"]["networks"]["proxy_net"]["ipv4_address"])

    def test_edge_replaces_instead_of_appending_forwarding_headers(self):
        template = (ROOT / "config/nginx/templates/default.conf.template").read_text()
        self.assertNotIn("$proxy_add_x_forwarded_for", template)
        self.assertEqual(template.count("proxy_set_header X-Forwarded-For $remote_addr;"), 2)
        self.assertEqual(template.count('proxy_set_header Forwarded "";'), 2)


if __name__ == "__main__":
    unittest.main()
