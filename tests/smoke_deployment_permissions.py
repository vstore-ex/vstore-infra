"""Linux UID isolation and app bind mounts in a disposable Docker volume."""
import subprocess
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parents[1]
volume = 'deployment-permissions-' + uuid.uuid4().hex[:12]


def run(*args):
    return subprocess.check_output(args, text=True).strip()


probe = r'''
import os, pathlib, stat, subprocess, sys
root = pathlib.Path('/fixture/deploy')
root.mkdir(mode=0o777)
root.chmod(0o777)
(root/'.env').write_text('ONLY_DISPOSABLE_FIXTURE=not-a-production-secret\n')
(root/'.env').chmod(0o644)
(root/'backups').mkdir()
(root/'backups/old.sql.gz').write_bytes(b'disposable-snapshot')
(root/'backups/old.sql.gz').chmod(0o644)
(root/'backups/old.sql.gz.metadata.json').write_text('{}')
(root/'storage').mkdir(mode=0o750)
os.chown(root/'storage',10001,10001)
(root/'proxy-config').mkdir(mode=0o755)
(root/'proxy-config/template').write_text('fixture-config')
(root/'proxy-config/entrypoint.sh').write_text('#!/bin/sh\ntest -r /config/template\n')
(root/'proxy-config/entrypoint.sh').chmod(0o755)
for path in (root/'proxy-config',root/'proxy-config/template',root/'proxy-config/entrypoint.sh'):
    os.chown(path,10002,10002)
for path in (root,root/'.env',root/'backups',root/'backups/old.sql.gz',root/'backups/old.sql.gz.metadata.json'):
    os.chown(path,10002,10002)
def uid(value):
    def drop():
        os.setgroups([])
        os.setgid(value)
        os.setuid(value)
    return drop
check = "import os; assert os.access('/fixture/deploy/.env',os.R_OK); assert os.access('/fixture/deploy/backups/old.sql.gz',os.R_OK); assert os.access('/fixture/deploy',os.W_OK)"
subprocess.run([sys.executable,'-c',check],preexec_fn=uid(65534),check=True)
rejected = subprocess.run([sys.executable,'/secure.py','--directory',str(root)],preexec_fn=uid(65534),capture_output=True,text=True)
assert rejected.returncode != 0
assert stat.S_IMODE(root.stat().st_mode) == 0o777
assert 'not-a-production-secret' not in rejected.stdout + rejected.stderr
subprocess.run([sys.executable,'/secure.py','--directory',str(root)],preexec_fn=uid(10002),check=True)
subprocess.run([sys.executable,'/secure.py','--directory',str(root),'--check'],preexec_fn=uid(10002),check=True)
for operation in ("open('/fixture/deploy/.env','rb')", "open('/fixture/deploy/backups/old.sql.gz','rb')", "open('/fixture/deploy/injected','w')"):
    code = 'try:\n '+operation+'\nexcept PermissionError:\n pass\nelse:\n raise AssertionError("unrelated UID retained access")'
    subprocess.run([sys.executable,'-c',code],preexec_fn=uid(65534),check=True)
subprocess.run([sys.executable,'-c',"import os; os.umask(0o077); open('/fixture/deploy/backups/new.sql.gz','wb').write(b'fixture')"],preexec_fn=uid(10002),check=True)
assert stat.S_IMODE((root/'backups/new.sql.gz').stat().st_mode)==0o600
assert (root/'storage').stat().st_uid==10001
assert stat.S_IMODE((root/'storage').stat().st_mode)==0o750
print('PASS: unrelated UID loses secret/snapshot read and deployment write; deployment owner can create private snapshots',flush=True)
'''

try:
    run('docker', 'volume', 'create', volume)
    subprocess.run(['docker', 'run', '--rm', '--network', 'none',
                    '-v', volume + ':/fixture',
                    '-v', str(ROOT / 'scripts/secure-deployment.py') + ':/secure.py:ro',
                    'python:3.12-alpine', 'python3', '-c', probe], check=True)
    # Docker resolves the host path as root; the app receives the mounted storage
    # directory without needing to traverse the private deployment parent.
    mountpoint = run('docker', 'volume', 'inspect', '--format', '{{.Mountpoint}}', volume)
    subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--user', '10001:10001',
                    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                    '--mount', 'type=bind,src=' + mountpoint + '/deploy/storage,dst=/storage',
                    'alpine:3.24', 'sh', '-c', 'echo fixture > /storage/media-probe; test -r /storage/media-probe'], check=True)
    print('PASS: app UID 10001 still writes its Docker bind-mounted storage below the private deployment directory', flush=True)
    subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--user', '0:0',
                    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                    '--mount', 'type=bind,src=' + mountpoint + '/deploy/proxy-config,dst=/config,readonly',
                    'alpine:3.24', '/config/entrypoint.sh'], check=True)
    print('PASS: proxy UID 0 without DAC_OVERRIDE still reads and executes nonsecret config mounts', flush=True)
finally:
    subprocess.run(['docker', 'volume', 'rm', volume], check=False)
