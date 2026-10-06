#!/usr/bin/env python3
"""Verified application and edge releases in an automatically loaded Compose override."""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

STATE = Path("docker-compose.override.yml")
APP_GROUPS = {
    "BACKEND_IMAGE": ("backend", "worker"),
    "FRONTEND_IMAGE": ("frontend",),
    "ADMIN_IMAGE": ("admin",),
}
GROUPS = {**APP_GROUPS, "PROXY_IMAGE": ("proxy",)}
HEALTH_URLS = {
    "BACKEND_IMAGE": "http://127.0.0.1:8080/health",
    "FRONTEND_IMAGE": "http://127.0.0.1:3000/healthz",
    "ADMIN_IMAGE": "http://127.0.0.1:3001/healthz",
}
DIGEST = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z")


def immutable(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ValueError("Release images must be immutable name@sha256 references")
    return value


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def document(current, previous):
    return {
        "x-vstore-release": {"version": 2 if "PROXY_IMAGE" in current else 1, "previous": previous},
        "services": {
            service: {"image": immutable(current[key])}
            for key, services in GROUPS.items() if key in current for service in services
        },
    }


def load():
    if not STATE.exists():
        raise ValueError("No release state; run scripts/compose.sh bootstrap first")
    data = json.loads(STATE.read_text())
    current = {key: data["services"][services[0]]["image"] for key, services in APP_GROUPS.items()}
    if "proxy" in data["services"]:
        current["PROXY_IMAGE"] = data["services"]["proxy"]["image"]
    previous = data["x-vstore-release"]["previous"]
    if set(previous) != set(current):
        raise ValueError("Invalid previous release keys")
    for value in previous.values():
        if not isinstance(value, str):
            raise ValueError("Invalid previous release image")
        if value:
            immutable(value)
    if data != document(current, previous):
        raise ValueError("Invalid release state; do not edit the generated override")
    return current, previous


def save(current, previous):
    """Replace one file containing both releases only after all verification succeeds."""
    content = json.dumps(document(current, previous), indent=2) + "\n"
    fd, name = tempfile.mkstemp(prefix=".release-state-", dir=".")
    backup = None
    preserve_backup = False
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if STATE.exists():
            original = STATE.read_bytes()
            backup_fd, backup = tempfile.mkstemp(prefix=".release-state-", dir=".")
            with os.fdopen(backup_fd, "wb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
        # Open before publication: even failure to open the directory must leave
        # the previously successful release visible to callers handling errors.
        directory = os.open(".", os.O_RDONLY)
        try:
            os.replace(name, STATE)
            try:
                os.fsync(directory)
            except OSError:
                # Callers restore containers when publication reports failure.
                # Restore the visible state first so those images stay aligned.
                try:
                    if backup is None:
                        STATE.unlink()
                    else:
                        os.replace(backup, STATE)
                except OSError as restore_error:
                    preserve_backup = True
                    raise OSError(
                        f"Release publication and state restoration failed; "
                        f"saved previous state: {backup or 'none (initial bootstrap)'}"
                    ) from restore_error
                # Keep the original publication error even if the device cannot
                # sync the restoration either. The previous file is visible.
                with contextlib.suppress(OSError):
                    os.fsync(directory)
                raise
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)
        if backup is not None and not preserve_backup and os.path.exists(backup):
            os.unlink(backup)


def running_image(service, expected=None, healthy=True):
    # Read-only Compose discovery never converges a stack or prints its secrets.
    container = docker("compose", "ps", "-q", service)
    if not container or "\n" in container:
        raise ValueError(f"Expected exactly one running {service} container")
    state = json.loads(docker("inspect", "--format", "{{json .State}}", container))
    if not state.get("Running") or (healthy and service != "worker" and state.get("Health", {}).get("Status") != "healthy"):
        raise ValueError(f"{service} is not healthy (worker requires running state)")
    # RepoDigests belongs to the image, not the container.
    image_id = docker("inspect", "--format", "{{.Image}}", container)
    digests = json.loads(docker("image", "inspect", "--format", "{{json .RepoDigests}}", image_id)) or []
    if expected:
        immutable(expected)
        if expected not in digests or docker("image", "inspect", "--format", "{{.Id}}", expected) != image_id:
            raise ValueError(f"{service} does not run the expected release image")
        return expected
    configured = docker("inspect", "--format", "{{.Config.Image}}", container)
    if DIGEST.fullmatch(configured) and configured in digests:
        return configured
    # Match the container's repository when a tag was used for the initial install.
    repository = configured.split("@")[0]
    if ":" in repository.rsplit("/", 1)[-1]:
        repository = repository.rsplit(":", 1)[0]
    matches = [ref for ref in digests if ref.split("@")[0] == repository and DIGEST.fullmatch(ref)]
    if len(matches) != 1:
        raise ValueError(f"{service} has no unambiguous registry digest; adoption refused")
    return matches[0]


def verify(current, healthy=True):
    for key, image in current.items():
        for service in GROUPS[key]:
            running_image(service, image, healthy=healthy)


def bootstrap(if_missing=False, proxy_image=None):
    if STATE.exists():
        load()
        if if_missing:
            return
        raise ValueError("Release state already exists; bootstrap refuses to overwrite it")
    current = {}
    for key, services in APP_GROUPS.items():
        current[key] = running_image(services[0])
        for service in services[1:]:
            running_image(service, current[key])
    if proxy_image:
        current["PROXY_IMAGE"] = running_image("proxy", immutable(proxy_image))
    save(current, dict.fromkeys(current, ""))


def commit(key, image):
    current, previous = load()
    candidate = {**current, key: immutable(image)}
    verify(candidate)
    if image != current.get(key):
        previous[key] = current.get(key, "")
    save(candidate, previous)


def candidate_compose(current, *args):
    # Deliberately omit the successful-release override while trying a candidate.
    subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", *args],
        env={**os.environ, **current}, check=True,
    )


def check_http(key):
    for attempt in range(15):
        try:
            with urllib.request.urlopen(HEALTH_URLS[key], timeout=5) as response:
                if response.status == 200:
                    return
        except (OSError, ValueError):
            pass
        if attempt < 14:
            time.sleep(3)
    raise ValueError("Release HTTP health check failed")


def recover_proxies():
    subprocess.run([sys.executable, "scripts/proxy-health.py", "recover"], check=True)


def rollback(key):
    current, previous = load()
    if not previous.get(key):
        raise ValueError("No previous successful image is recorded for this service")
    candidate = {**current, key: previous[key]}
    services = GROUPS[key]
    candidate_compose(candidate, "pull", *services)
    try:
        candidate_compose(candidate, "up", "-d", "--no-deps", "--wait", "--wait-timeout", "150", *services)
        if key != "PROXY_IMAGE":
            check_http(key)
        recover_proxies()
        commit(key, candidate[key])
    except Exception:
        candidate_compose(current, "up", "-d", "--no-deps", "--wait", "--wait-timeout", "150", *services)
        if key != "PROXY_IMAGE":
            check_http(key)
        recover_proxies()
        raise


@contextlib.contextmanager
def locked(inherited_fd):
    with open(".deploy.lock", "a") as stream:
        if inherited_fd is None:
            deadline = time.monotonic() + 300
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Timeout waiting for deployment lock")
                    time.sleep(0.1)
        else:
            # Reuse the workflow's shared flock rather than deadlocking on a new descriptor.
            actual, inherited = os.fstat(stream.fileno()), os.fstat(inherited_fd)
            if (actual.st_dev, actual.st_ino) != (inherited.st_dev, inherited.st_ino):
                raise ValueError("Inherited deployment lock does not match this stack")
            fcntl.flock(inherited_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield inherited_fd if inherited_fd is not None else stream.fileno()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-fd", type=int)
    commands = parser.add_subparsers(dest="command", required=True)
    adoption = commands.add_parser("bootstrap")
    adoption.add_argument("--if-missing", action="store_true")
    adoption.add_argument("--proxy-image", help="Include an already running, reviewed edge registry digest")
    commands.add_parser("env")
    commands.add_parser("verify")
    commands.add_parser("verify-images")
    commands.add_parser("recover")
    publish = commands.add_parser("commit")
    publish.add_argument("key", choices=GROUPS)
    publish.add_argument("image")
    undo = commands.add_parser("rollback")
    undo.add_argument("key", choices=GROUPS)
    compose = commands.add_parser("compose")
    compose.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    # A log follower must not prevent deployments by holding the lock indefinitely.
    read_only = args.command == "compose" and args.args and args.args[0] in (
        "logs", "ps", "config", "images", "top", "events", "version",
    )
    with (contextlib.nullcontext() if read_only else locked(args.lock_fd)) as lock_fd:
        if args.command == "bootstrap":
            bootstrap(args.if_missing, args.proxy_image)
        elif args.command == "commit":
            commit(args.key, args.image)
        elif args.command == "rollback":
            rollback(args.key)
        else:
            current, _ = load()
            if args.command == "env":
                for key, value in current.items():
                    print(f"{key}={value}")
            elif args.command == "recover":
                recover_proxies()
            elif args.command in ("verify", "verify-images"):
                verify(current, healthy=args.command == "verify")
            else:
                # The default override also protects plain docker compose in fresh sessions.
                if args.args and args.args[0] in ("up", "restart", "start"):
                    subprocess.run([sys.executable, "scripts/redis-policy.py", "--lock-fd",
                                    str(lock_fd), "check"], pass_fds=(lock_fd,), check=True)
                result = subprocess.call(["docker", "compose", *args.args])
                if result == 0 and args.args and args.args[0] in ("up", "restart", "start"):
                    recover_proxies()
                sys.exit(result)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
