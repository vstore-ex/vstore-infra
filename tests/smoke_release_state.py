"""Exercise adoption, SSH deployment, fresh-session convergence and rollback on Docker."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release", ROOT / "scripts/release-state.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def images():
    refs = []
    for tag in ("nginx:1.27-alpine", "nginx:alpine"):
        run("docker", "pull", tag)
        refs.append(json.loads(run("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", tag))[0])
    if refs[0] == refs[1]:
        raise RuntimeError("Smoke test needs two distinct registry images")
    return refs


def expected(ref):
    config = json.loads(run("docker", "compose", "config", "--format", "json"))
    for service in ("backend", "worker", "admin"):
        assert config["services"][service]["image"] == old, service
    assert config["services"]["frontend"]["image"] == ref
    release.verify(release.load()[0])


def workflow_script(name, directory):
    workflow = (ROOT / ".github/workflows" / name).read_text()
    script = workflow.split("          script: |\n", 1)[1]
    return "\n".join(line[12:] for line in script.splitlines()).replace('${{ secrets.DEPLOY_PATH }}', directory)


old, new = images()
project = "release-smoke-" + uuid.uuid4().hex[:12]
os.environ["COMPOSE_PROJECT_NAME"] = project
os.environ["LC_ALL"] = "C"
original_directory = Path.cwd()
with tempfile.TemporaryDirectory(prefix="vstore-release-smoke-") as directory:
    os.chdir(directory)
    try:
        shutil.copytree(ROOT / "scripts", "scripts")
        # A01 fixture has only static HTTP servers; real TLS/API recovery is
        # exercised separately by smoke_upstream_recovery.py.
        Path("scripts/proxy-health.py").write_text(
            "import os\nraise SystemExit(1 if os.getenv('FAIL_GATE_IMAGE') and "
            "os.getenv('FRONTEND_IMAGE') == os.getenv('FAIL_GATE_IMAGE') else 0)\n")
        Path(".gitignore").write_text((ROOT / ".gitignore").read_text() + "\nremote.git/\nbin/\n")
        services = {
            service: {
                "image": "${" + key + ":-" + old + "}",
                "healthcheck": {"test": ["CMD", "wget", "-qO-", "http://127.0.0.1/"], "interval": "1s", "timeout": "2s", "retries": 10},
            }
            for key, group in release.GROUPS.items() for service in group
        }
        # Nginx has no inherited healthcheck. Omit the fixture's check entirely:
        # older Compose versions treat disable:true as a check during --wait.
        services["worker"].pop("healthcheck")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port_number = reservation.getsockname()[1]
        services["frontend"]["ports"] = [f"127.0.0.1:{port_number}:80"]
        proxy_image = project + "-proxy:local"
        services["proxy"] = {
            "image": proxy_image, "build": {"context": "config/nginx"},
            "healthcheck": services["admin"]["healthcheck"],
            "volumes": ["./config/nginx/templates:/etc/nginx/templates:ro"],
        }
        Path("config/nginx/templates").mkdir(parents=True)
        dockerfile = Path("config/nginx/Dockerfile")
        template = Path("config/nginx/templates/release.conf.template")

        def proxy_fixture(marker):
            dockerfile.write_text("FROM " + old + "\nENV VSTORE_PROXY_RELEASE=" + marker + "\n")
            template.write_text('server { listen 8081; location /release { return 200 "' + marker + '"; } }\n')

        def proxy_expected(marker):
            container = run('docker', 'compose', 'ps', '-q', 'proxy')
            environment_values = json.loads(run('docker', 'inspect', '--format', '{{json .Config.Env}}', container))
            assert 'VSTORE_PROXY_RELEASE=' + marker in environment_values, environment_values
            assert run('docker', 'compose', 'exec', '-T', 'proxy', 'wget', '-qO-',
                       'http://127.0.0.1:8081/release') == marker

        proxy_fixture('initial')
        Path("docker-compose.yml").write_text(json.dumps({"services": services}))
        Path(".env").write_text("\n".join(f"{key}={old}" for key in release.GROUPS) + "\n")
        run("git", "init", "--initial-branch=main")
        run("git", "config", "user.name", "Release smoke test")
        run("git", "config", "user.email", "release-smoke@example.invalid")
        run("git", "add", "scripts", "docker-compose.yml", ".gitignore", "config")
        run("git", "commit", "-m", "test: initialize disposable stack")
        run("git", "init", "--bare", "--initial-branch=main", "remote.git")
        run("git", "remote", "add", "origin", str(Path("remote.git").resolve()))
        # The remote infra commit changes config while the host stays at the old commit.
        services["frontend"]["labels"] = {"release-smoke-config": "updated"}
        Path("docker-compose.yml").write_text(json.dumps({"services": services}))
        proxy_fixture('updated')
        run("git", "add", "docker-compose.yml", "config")
        run("git", "commit", "-m", "test: update disposable infra configuration")
        run("git", "push", "origin", "main")
        run("git", "reset", "--hard", "HEAD^")
        run("docker", "compose", "build", "proxy")
        run("docker", "compose", "up", "-d", "--wait", "--wait-timeout", "60")
        run("bash", "scripts/compose.sh", "bootstrap")
        expected(old)
        port = run("docker", "compose", "port", "frontend", "80")
        health = "http://" + port + "/"

        # Exercise the actual reusable workflow script, with pruning suppressed so
        # the test never removes unrelated images from the developer's Docker host.
        script = workflow_script("deploy-service.yml", directory)
        infra_script = workflow_script("ci.yml", directory)
        docker_path = shutil.which("docker")
        Path("bin").mkdir()
        shim = Path("bin/docker")
        shim.write_text(f'#!/usr/bin/env bash\nif [ "$1 ${{2:-}}" = "image prune" ]; then exit 0; fi\nexec "{docker_path}" "$@"\n')
        shim.chmod(0o755)
        if not shutil.which("flock"):
            shim = Path("bin/flock")
            shim.write_text('#!/usr/bin/env python3\nimport fcntl,sys\nfcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)\n')
            shim.chmod(0o755)
        environment = {
            **os.environ, "PATH": str(Path("bin").resolve()) + ":" + os.environ["PATH"],
            "IMAGE": new, "IMAGE_VAR": "FRONTEND_IMAGE", "SERVICES": "frontend",
            "HEALTH_URL": health, "RUN_MIGRATIONS": "false",
        }
        subprocess.run(["bash", "-c", script], env=environment, check=True)
        expected(new)
        assert release.load()[1]["FRONTEND_IMAGE"] == old

        # A fresh shell has stale .env pins and even a stale exported image.
        fresh = {**os.environ, "FRONTEND_IMAGE": old}
        run("bash", "scripts/compose.sh", "up", "-d", "--wait", "--wait-timeout", "60", env=fresh)
        subprocess.run(["bash", "-c", infra_script], env={**fresh, "PATH": environment["PATH"]}, check=True)
        expected(new)
        proxy_expected('updated')
        assert json.loads(run("docker", "compose", "config", "--format", "json"))["services"]["frontend"]["labels"]["release-smoke-config"] == "updated"
        print("PASS: app deployment survives fresh-session manual/infra convergence")

        # A template-only update must render even though Compose's service hash
        # and the locally built image do not change.
        template.write_text('server { listen 8081; location /release { return 200 "template-only"; } }\n')
        run('git', 'add', str(template))
        run('git', 'commit', '-m', 'test: change only edge template')
        run('git', 'push', 'origin', 'main')
        run('git', 'reset', '--hard', 'HEAD^')
        subprocess.run(['bash', '-c', infra_script], env={**fresh, 'PATH': environment['PATH']}, check=True)
        assert run('docker', 'compose', 'exec', '-T', 'proxy', 'wget', '-qO-',
                   'http://127.0.0.1:8081/release') == 'template-only'
        print('PASS: template-only infra update renders the new edge config')

        release.HEALTH_URLS["FRONTEND_IMAGE"] = health
        with release.locked(None):
            release.rollback("FRONTEND_IMAGE")
        subprocess.run(["bash", "-c", infra_script], env={**fresh, "PATH": environment["PATH"]}, check=True)
        expected(old)
        assert release.load()[1]["FRONTEND_IMAGE"] == new
        print("PASS: intentional rollback survives the next fresh session")

        # An unhealthy candidate must restore the last verified release and leave
        # successful history byte-for-byte unchanged.
        before = release.STATE.read_bytes()
        environment["HEALTH_URL"] = "http://127.0.0.1:1/"
        # Remove retries only in this test to avoid spending a minute on known failure.
        failure_script = script.replace("RETRIES=15", "RETRIES=1").replace("sleep 3", "sleep 0").replace("--retry 14", "--retry 0")
        result = subprocess.run(["bash", "-c", failure_script], env=environment)
        assert result.returncode != 0
        assert release.STATE.read_bytes() == before
        expected(old)
        print("PASS: failed HTTP gate never publishes the candidate")

        environment['HEALTH_URL'] = health
        environment['FAIL_GATE_IMAGE'] = new
        result = subprocess.run(['bash', '-c', failure_script], env=environment)
        assert result.returncode != 0
        assert release.STATE.read_bytes() == before
        expected(old)
        del environment['FAIL_GATE_IMAGE']
        print('PASS: failed proxy API gate restores the image without publishing the candidate')

        # A terminated deployment can leave candidate containers, but no candidate
        # success record. The next infra session restores the verified release.
        candidate = {**release.load()[0], "FRONTEND_IMAGE": new}
        release.candidate_compose(candidate, "up", "-d", "--no-deps", "--wait", "frontend")
        assert release.STATE.read_bytes() == before
        run("bash", "-c", 'exec 200>.deploy.lock; source scripts/release-state.sh; release_load; release_compose up -d --wait --wait-timeout 60; release_state verify')
        expected(old)
        print("PASS: interrupted candidate converges to the last verified release")

        # First rollout can need to restore a commit without proxy-health.py.
        # The cached gate must survive git reset and check restored config.
        run('git', 'rm', 'scripts/proxy-health.py')
        run('git', 'commit', '-m', 'test: legacy config without the API gate')
        legacy = run('git', 'rev-parse', 'HEAD')
        gate = Path('scripts/proxy-health.py')
        gate.write_text("import json,subprocess\n"
                        "data=json.loads(subprocess.check_output(['docker','compose','config','--format','json']))\n"
                        "raise SystemExit(1 if data['services']['frontend'].get('labels',{}).get('broken-api') else 0)\n")
        services['frontend']['labels']['broken-api'] = 'true'
        Path('docker-compose.yml').write_text(json.dumps({'services': services}))
        proxy_fixture('broken')
        run('git', 'add', 'scripts/proxy-health.py', 'docker-compose.yml', 'config')
        run('git', 'commit', '-m', 'test: candidate config with broken API')
        run('git', 'push', 'origin', 'main')
        run('git', 'reset', '--hard', legacy)
        result = subprocess.run(['bash', '-c', infra_script], env={**fresh, 'PATH': environment['PATH']})
        assert result.returncode != 0
        assert run('git', 'rev-parse', 'HEAD') == legacy
        assert not gate.exists()
        assert release.STATE.read_bytes() == before
        expected(old)
        container = run('docker', 'compose', 'ps', '-q', 'proxy')
        assert 'VSTORE_PROXY_RELEASE=updated' in json.loads(run('docker', 'inspect', '--format', '{{json .Config.Env}}', container))
        assert run('docker', 'compose', 'exec', '-T', 'proxy', 'wget', '-qO-',
                   'http://127.0.0.1:8081/release') == 'template-only'
        print('PASS: infra API gate rolls back config even when the legacy commit lacks the helper')
        print('PASS: config rollback rebuilds the previous proxy image and re-renders its template')
    finally:
        # Only the unique disposable project is removed; never touch production.
        subprocess.run(["docker", "compose", "down", "--volumes", "--remove-orphans"], check=False)
        subprocess.run(["docker", "image", "rm", project + "-proxy:local"], check=False)
        os.chdir(original_directory)
