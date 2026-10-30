"""Opt-in LOCAL disposable Docker VM only; privileged cgroup v2 regression.
Run: docker run --rm --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/host-cgroup -v "$PWD/tests:/tests:ro" \
  python:3.11-slim python /tests/smoke_monitoring_oom.py
Never run against production. Creates only a random bounded fixture subtree.
"""
import json
from pathlib import Path
import subprocess
import sys
import uuid

root = Path('/host-cgroup') / ('vstore-oom-test-' + uuid.uuid4().hex)
children = []
processes = []
try:
    root.mkdir()
    (root / 'cgroup.subtree_control').write_text('+memory')
    # Outer safety budget bounds all fixture children even if the inner guard fails.
    (root / 'memory.max').write_text(str(192 * 1024**2))
    (root / 'memory.swap.max').write_text('0')
    for name in ('app', 'monitor'):
        child = root / name
        child.mkdir()
        children.append(child)
    monitor = children[1]
    (monitor / 'memory.max').write_text(str(48 * 1024**2))
    (monitor / 'memory.swap.max').write_text('0')
    def start(group, code):
        # Child joins before allocating; parent/controller stays outside fixture.
        prefix = f"from pathlib import Path; Path({str(group / 'cgroup.procs')!r}).write_text(str(__import__('os').getpid())); "
        proc = subprocess.Popen([sys.executable, '-c', prefix + code])
        processes.append(proc)
        return proc
    app = start(children[0], "import time; data=bytearray(8*1024**2); time.sleep(60)")
    victim = start(monitor, "data=[]\nwhile True: data.append(bytearray(1024**2))")
    result = victim.wait(timeout=20)
    events = dict(line.split() for line in (monitor / 'memory.events').read_text().splitlines())
    assert result == -9, result
    assert int(events['oom_kill']) >= 1, events
    assert app.poll() is None, 'sibling application died'
    print(json.dumps({'monitor_exit': result, 'monitor_oom_kill': int(events['oom_kill']), 'sibling_alive': True}))
finally:
    for proc in processes:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    for child in reversed(children):
        child.rmdir()
    if root.exists():
        root.rmdir()
