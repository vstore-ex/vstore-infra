"""Force backend/app IP changes through real app configs and the TLS edge."""
import hashlib
import shutil
import socket
import sys
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
    compose_file = Path(directory) / "docker-compose.yml"
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
    fixture['services']['backend']['command'] = ['python', '/probe/backend.py']
    backend_file = Path(directory) / 'backend.py'
    backend_file.write_text("""from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('Upgrade', '').lower() == 'websocket':
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'upgrade')
            self.end_headers()
            return
        payload = json.dumps([] if self.path.startswith('/api/v1/games/') else {'path': self.path}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
""")
    fixture['services']['backend']['volumes'] = [str(backend_file) + ':/probe/backend.py:ro']
    fixture['services']['proxy']['ports'] = ['127.0.0.1::443']
    test_client = Path(directory) / 'client.py'
    test_client.write_text((FIXTURES / 'client.py').read_text().replace(
        'json.loads(response.read())', "response.read().decode(errors='replace')"))
    for name in ('client-a', 'client-b'):
        fixture['services'][name]['volumes'] = [str(test_client) + ':/probe/client.py:ro']
    compose_file.write_text(json.dumps(fixture))
    gate_environment = {**os.environ, 'COMPOSE_PROJECT_NAME': project}
    def gate(command='recover', success=True):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/proxy-health.py'), command,
                                 '--attempts', '1' if not success else '5', '--delay', '1'], cwd=directory, env=gate_environment)
        assert (result.returncode == 0) == success

    def status(host='vstore.local', path='/api/v1/games/?limit=1'):
        try:
            return client([request('https://proxy' + path, host=host)])[0]['status']
        except subprocess.CalledProcessError:
            return 0  # Old removed IP can time out before Nginx returns 502/504.

    try:
        compose('build', 'backend', 'proxy')
        compose('up', '-d', '--wait', '--wait-timeout', '120')
        gate()
        for service, offset in (('backend', 6), ('frontend', 7), ('admin', 8)):
            fixture['services'][service]['networks']['proxy_net']['ipv4_address'] = str(subnet.network_address + offset)
            compose_file.write_text(json.dumps(fixture))
            compose('up', '-d', '--no-deps', '--wait', service)
            assert status('vstore.local' if service != 'admin' else 'admin.vstore.local') in (0, 502, 504)
            # A static health endpoint remains green while the API is broken.
            assert 'healthy' in compose('exec', '-T', 'proxy', 'wget', '-qO-', 'http://127.0.0.1/healthz')
            gate('verify', success=False)
            gate()
            print('PASS: HTTPS API recovers after changed ' + service + ' IP', flush=True)
        for host in ('vstore.local', 'admin.vstore.local'):
            for path in ('/api/v1/me', '/api/v1/admin/users', '/static/probe'):
                assert status(host, path) == 200
            if host != 'admin.vstore.local':
                continue  # Storefront baseline does not forward Upgrade headers.
            result = client([{'url': 'https://proxy/api/probe', 'host': host,
                             'headers': [['Upgrade', 'websocket'], ['Connection', 'Upgrade']]}])[0]
            assert result['status'] == 101, result
        print('PASS: rewritten paths, media routes and WebSocket upgrades survive reload', flush=True)
        compose('stop', 'backend')
        gate('recover', success=False)
        assert 'healthy' in compose('exec', '-T', 'proxy', 'wget', '-qO-', 'http://127.0.0.1/healthz')
        print('PASS: broken API fails gate despite healthy static endpoints', flush=True)
    finally:
        subprocess.run(['docker', 'compose', '-p', project, '-f', str(compose_file), 'down', '--volumes', '--remove-orphans'], check=False)
        subprocess.run(['docker', 'image', 'rm', probe_image, edge_image], check=False)
