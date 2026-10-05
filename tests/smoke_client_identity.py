"""Real TLS edge/app/Uvicorn/Redis chain; no production auth or database writes."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/client-identity"
project = "identity-smoke-" + uuid.uuid4().hex[:12]


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def compose(*args):
    return run("docker", "compose", "-p", project, "-f", str(compose_file), *args)


def client(requests, name="client-a"):
    return json.loads(compose("exec", "-T", name, "python", "client.py", json.dumps(requests)))


def request(url, spoof="198.51.100.99", method="GET", host=None):
    return {"url": url, "host": host or "vstore.local", "method": method, "headers": [
        ["X-Forwarded-For", spoof], ["X-Real-IP", spoof], ["Forwarded", f"for={spoof}"],
    ]}


# Snapshot provenance keeps CI independent of private application-repo tokens.
for name, source in json.loads((FIXTURES / "sources.json").read_text()).items():
    assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == source["sha256"]

# Choose a free /28 without assuming the developer's Docker/VPN subnet layout.
existing = []
networks = run("docker", "network", "ls", "-q").splitlines()
if networks:
    for network in json.loads(run("docker", "network", "inspect", *networks)):
        for config in network.get("IPAM", {}).get("Config", []) or []:
            if config.get("Subnet"):
                existing.append(ipaddress.ip_network(config["Subnet"]))
for index in range(200, 256):
    subnet = ipaddress.ip_network(f"172.31.{index}.0/28")
    if not any(subnet.version == network.version and subnet.overlaps(network) for network in existing):
        break
else:
    raise RuntimeError("No free test proxy subnet")
addresses = [str(subnet.network_address + offset) for offset in (2, 3, 4, 10, 11)]
environment = {**os.environ, "PROXY_NET_SUBNET": str(subnet), "EDGE_PROXY_IP": addresses[0],
               "FRONTEND_PROXY_IP": addresses[1], "ADMIN_PROXY_IP": addresses[2],
               "BACKEND_PROXY_IP": str(subnet.network_address + 5)}
production = json.loads(run("docker", "compose", "--env-file", ".env.example", "config", "--format", "json", cwd=ROOT, env=environment))
services = production["services"]
probe_image, edge_image = project + "-probe:local", project + "-edge:local"

with tempfile.TemporaryDirectory(prefix="vstore-identity-smoke-") as directory:
    compose_file = Path(directory) / "compose.json"
    backend_command = services["backend"]["command"].copy()
    backend_command[1] = "probe:app"
    health = {"test": ["CMD", "wget", "-qO-", "http://127.0.0.1:8080/healthz"], "interval": "1s", "timeout": "2s", "retries": 20}
    fixture = {
        "networks": {"app_net": {}, "proxy_net": {"ipam": {"config": [{"subnet": str(subnet)}]}}},
        "volumes": {"certs": {}, "webroot": {}},
        "services": {
            "redis": {"image": services["redis"]["image"], "networks": ["app_net"],
                      "healthcheck": {"test": ["CMD", "redis-cli", "ping"], "interval": "1s", "timeout": "2s", "retries": 20}},
            "backend": {"image": probe_image, "build": {"context": str(FIXTURES)}, "command": backend_command,
                        "environment": {"TRUST_PROXY_HEADERS": services["backend"]["environment"]["TRUST_PROXY_HEADERS"]},
                        "networks": services["backend"]["networks"], "depends_on": {"redis": {"condition": "service_healthy"}},
                        "healthcheck": {"test": ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"], "interval": "1s", "timeout": "2s", "retries": 20}},
        },
    }
    for name in ("frontend", "admin"):
        fixture["services"][name] = {
            "image": "nginx:1.27-alpine", "networks": services[name]["networks"], "healthcheck": health,
            "volumes": [str(FIXTURES / (name + ".conf")) + ":/etc/nginx/conf.d/default.conf:ro"],
            "depends_on": {"backend": {"condition": "service_healthy"}},
        }
    fixture["services"]["proxy"] = {
        "image": edge_image, "build": {"context": str(ROOT / "config/nginx")},
        "environment": services["proxy"]["environment"], "networks": services["proxy"]["networks"],
        "cap_drop": services["proxy"]["cap_drop"], "cap_add": services["proxy"]["cap_add"],
        "security_opt": services["proxy"]["security_opt"],
        "volumes": [str(ROOT / "config/nginx/templates") + ":/etc/nginx/templates:ro",
                    str(ROOT / "config/nginx/entrypoint/10-init-dummy-certs.sh") + ":/docker-entrypoint.d/10-init-dummy-certs.sh:ro",
                    "certs:/etc/letsencrypt", "webroot:/var/www/certbot"],
        "healthcheck": {**health, "test": ["CMD", "wget", "-qO-", "http://127.0.0.1/healthz"]},
        "depends_on": {name: {"condition": "service_healthy"} for name in ("frontend", "admin")},
    }
    for name, address in (("client-a", addresses[3]), ("client-b", addresses[4])):
        fixture["services"][name] = {"image": probe_image, "command": ["sleep", "infinity"],
                                     "cap_drop": ["ALL"], "networks": {"proxy_net": {"ipv4_address": address}}}
    fixture["services"]["untrusted"] = {"image": probe_image, "command": ["sleep", "infinity"], "cap_drop": ["ALL"], "networks": ["app_net"]}
    compose_file.write_text(json.dumps(fixture))
    try:
        compose("build", "backend", "proxy")
        compose("up", "-d", "--wait", "--wait-timeout", "120")
        for name in ("proxy", "frontend", "admin"):
            compose("exec", "-T", name, "nginx", "-t")

        for host in ("vstore.local", "admin.vstore.local"):
            variants = ["198.51.100.99", "203.0.113.9, 192.0.2.10", "2001:db8::9", "unknown", addresses[0]]
            requests = [request("https://proxy/api/identity", value, host=host) for value in variants]
            duplicate = request("https://proxy/api/identity", host=host)
            duplicate["headers"].append(["X-Forwarded-For", "203.0.113.250"])
            requests.append(duplicate)
            for result in client(requests):
                assert result["status"] == 200 and result["data"]["ip"] == addresses[3], result
                assert result["data"]["xff"] == addresses[3] + ", " + addresses[0], result
                assert result["data"]["forwarded"] is None, result
            second = client([request("https://proxy/api/identity", host=host)], "client-b")[0]
            assert second["data"]["ip"] == addresses[4], second
        print("PASS: both TLS chains replace spoofed/duplicate XFF and preserve distinct clients", flush=True)

        for target in ("http://frontend:8080", "http://admin:8080", "http://backend:8000"):
            for result in client([request(target + "/api/identity", "198.51.100." + str(index)) for index in range(1, 5)]):
                assert result["status"] == 200 and result["data"]["ip"] == addresses[3], result
        outsider = client([request("http://backend:8000/api/identity", "203.0.113.88")], "untrusted")[0]
        assert outsider["status"] == 200 and outsider["data"]["ip"] != "203.0.113.88", outsider
        print("PASS: direct app/backend and data-network requests cannot claim a forwarded identity", flush=True)

        routes = [("/api/v1/auth/login", 5), ("/api/v1/auth/register", 5), ("/api/v1/media/upload", 20)]
        for target, host in (("https://proxy", "vstore.local"), ("https://proxy", "admin.vstore.local"),
                             ("http://frontend:8080", "vstore.local"), ("http://admin:8080", "admin.vstore.local"),
                             ("http://backend:8000", "vstore.local")):
            for path, limit in routes:
                compose("exec", "-T", "redis", "redis-cli", "-n", "2", "FLUSHDB")
                results = client([request(target + path, f"198.51.100.{index}", "POST", host) for index in range(1, limit + 2)])
                assert [item["status"] for item in results] == [200] * limit + [429], results
                assert results[-1]["retry"] is not None
                second = client([request(target + path, "198.51.100.99", "POST", host)], "client-b")[0]
                assert second["status"] == 200 and second["data"]["ip"] == addresses[4], second
                keys = compose("exec", "-T", "redis", "redis-cli", "-n", "2", "KEYS", "rate_limit:*").splitlines()
                assert set(keys) == {f"rate_limit:{path}:{addresses[3]}", f"rate_limit:{path}:{addresses[4]}"}, keys
        print("PASS: real Redis enforces login/registration/upload limits on all five ingress paths", flush=True)
    finally:
        subprocess.run(["docker", "compose", "-p", project, "-f", str(compose_file), "down", "--volumes", "--remove-orphans"], check=False)
        subprocess.run(["docker", "image", "rm", probe_image, edge_image], check=False)
