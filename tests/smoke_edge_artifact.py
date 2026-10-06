"""Publish/pull/run the scanned artifact in a disposable localhost registry."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
image = sys.argv[1]
name = 'edge-artifact-' + uuid.uuid4().hex[:12]


def run(*args):
    return subprocess.check_output(args, text=True).strip()


identity = run('docker', 'image', 'inspect', image, '--format', '{{.Id}}')
with socket.socket() as reservation:
    reservation.bind(('127.0.0.1', 0))
    port = reservation.getsockname()[1]
repository = f'localhost:{port}/edge-fixture'
tag = repository + ':scanned'
reference = None
try:
    run('docker', 'run', '-d', '--name', name, '-p', f'127.0.0.1:{port}:5000',
        '--tmpfs', '/var/lib/registry', 'registry:2')
    for attempt in range(30):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/v2/', timeout=2) as response:
                assert response.status == 200
            break
        except OSError:
            if attempt == 29:
                raise
            time.sleep(1)
    run('docker', 'tag', identity, tag)
    run('docker', 'push', tag)
    digests = json.loads(run('docker', 'image', 'inspect', tag, '--format', '{{json .RepoDigests}}'))
    matches = [ref for ref in digests if ref.startswith(repository + '@sha256:')]
    assert len(matches) == 1, matches
    reference = matches[0]
    run('docker', 'pull', '--platform', 'linux/amd64', reference)
    assert run('docker', 'image', 'inspect', reference, '--format', '{{.Id}}') == identity
    subprocess.run(['bash', 'tests/smoke-proxy.sh', reference], cwd=ROOT, check=True)
    print('PASS: scanned image ID equals published/pulled digest artifact; TLS bootstrap runs that digest', flush=True)
finally:
    subprocess.run(['docker', 'rm', '-f', name], check=False)
    subprocess.run(['docker', 'image', 'rm', tag], check=False)
    if reference:
        subprocess.run(['docker', 'image', 'rm', reference], check=False)
