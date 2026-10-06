"""PostgreSQL 17 recovery drill; only a uniquely named disposable Compose stack."""
import gzip
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
project = 'recovery-smoke-' + uuid.uuid4().hex[:12]

with tempfile.TemporaryDirectory(prefix='vstore-database-recovery-') as directory:
    directory = Path(directory)
    shutil.copytree(ROOT / 'scripts', directory / 'scripts')
    fixture = {'services': {
        'postgres': {'image': 'postgres:17', 'environment': {'POSTGRES_USER': 'postgres', 'POSTGRES_DB': 'app',
                                                               'POSTGRES_PASSWORD': 'disposable-fixture-only'},
                     'healthcheck': {'test': ['CMD', 'pg_isready', '-U', 'postgres', '-d', 'app'],
                                     'interval': '1s', 'timeout': '2s', 'retries': 30}},
        'backend': {'image': 'postgres:17', 'environment': {'DATABASE_URL': 'postgresql://postgres:disposable-fixture-only@postgres:5432/live_app'},
                    'entrypoint': ['sh', '-c', 'echo "[entrypoint] unrelated startup output"; exec sleep infinity']},
        'worker': {'image': 'postgres:17', 'entrypoint': ['sleep', 'infinity']}}}
    (directory / 'docker-compose.yml').write_text(json.dumps(fixture))
    environment = {**os.environ, 'COMPOSE_PROJECT_NAME': project}

    def run(*args, **kwargs):
        return subprocess.check_output(args, cwd=directory, env=environment, **kwargs)

    def compose(*args, **kwargs):
        return run('docker', 'compose', *args, **kwargs)

    def sql(query, database='live_app'):
        return compose('exec', '-T', 'postgres', 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1',
                       '-U', 'postgres', '-d', database, input=query, text=True).strip()

    def helper(*args, success=True):
        result = subprocess.run([sys.executable, 'scripts/database-recovery.py', *map(str, args)],
                                cwd=directory, env=environment, capture_output=True, text=True)
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        if success:
            return result.stdout.strip()
        print('Expected failure: ' + result.stderr.strip().splitlines()[-1], flush=True)

    def snapshot(name, revision='a1', extra=b''):
        path = directory / (name + '.sql.gz')
        dump = compose('exec', '-T', 'postgres', 'pg_dump', '-U', 'postgres', '-d', 'live_app')
        with gzip.open(path, 'wb') as stream:
            stream.write(dump + extra)
        helper('record', path, '--revision', revision)
        return path

    checks = directory / 'verify.sql'
    checks.write_text("""DO $$ BEGIN
IF (SELECT count(*) FROM recovery_probe) <> 1 OR
   (SELECT value FROM recovery_probe WHERE id=1) IS DISTINCT FROM 'before' THEN
    RAISE EXCEPTION 'snapshot data does not match expected baseline';
END IF;
END $$;
""")

    def restore(path, name, success=True, verify=checks):
        return helper('restore', path, '--target', name, '--verify-sql', verify, '--writers-stopped', success=success)

    try:
        compose('up', '-d', '--wait', '--wait-timeout', '90')
        sql('CREATE DATABASE live_app TEMPLATE template0;', 'postgres')
        assert sql('SHOW server_version_num;').startswith('17')
        assert helper('source-database') == 'live_app'
        assert helper('revision', '--database', 'app') == 'base'
        sql("CREATE TABLE recovery_probe(id integer PRIMARY KEY, value text NOT NULL);\n"
            "INSERT INTO recovery_probe VALUES (1, 'before');\n"
            "CREATE TABLE alembic_version(version_num varchar(32) PRIMARY KEY);\n"
            "INSERT INTO alembic_version VALUES ('a1');")
        assert helper('revision') == 'a1'
        sql("INSERT INTO alembic_version VALUES ('z9');")
        assert helper('revision') == 'a1,z9'
        sql("DELETE FROM alembic_version WHERE version_num='z9';")
        # Execute the actual reusable workflow's pre-migration snapshot block,
        # including inherited deployment lock and revision/image sidecar.
        workflow = (ROOT / '.github/workflows/deploy-service.yml').read_text()
        block = workflow.split('            if [ "$RUN_MIGRATIONS" = "true" ]; then\n', 1)[1]
        block = block.split('              echo "==> Applying database migrations..."', 1)[0]
        script = 'set -euo pipefail\nsource scripts/release-state.sh\n' + '\n'.join(line[12:] for line in block.splitlines())
        image = json.loads(run('docker', 'image', 'inspect', '--format', '{{json .RepoDigests}}', 'postgres:17', text=True))[0]
        with open(directory / '.deploy.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            os.dup2(lock.fileno(), 200)
            try:
                subprocess.run(['bash', '-c', script], cwd=directory,
                               env={**environment, 'DEPLOY_PATH': str(directory), 'PREV_IMAGE': image},
                               pass_fds=(200,), check=True)
            finally:
                os.close(200)
        good = next((directory / 'backups').glob('*.sql.gz'))
        metadata = json.loads(Path(str(good) + '.metadata.json').read_text())
        assert metadata['revision'] == 'a1' and metadata['postgres_major'] == 17
        assert metadata['database'] == 'live_app'
        assert metadata['backend_image'] == image
        print('PASS: actual workflow snapshots DATABASE_URL database despite a different POSTGRES_DB', flush=True)
        error = snapshot('sql-error', extra=b'\nSELECT 1/0;\n')
        sql("UPDATE recovery_probe SET value='after'; INSERT INTO recovery_probe VALUES (2,'new');\n"
            "UPDATE alembic_version SET version_num='b2';")
        restore(good, 'recovery_running_writer', success=False)
        assert sql("SELECT count(*) FROM pg_database WHERE datname='recovery_running_writer';") == '0'
        compose('stop', 'backend', 'worker')
        assert 'PASS:' in restore(good, 'recovery_verified')
        assert sql('SELECT value FROM recovery_probe WHERE id=1;', 'recovery_verified') == 'before'
        assert helper('revision', '--database', 'recovery_verified') == 'a1'
        assert sql('SELECT count(*) FROM recovery_probe;') == '2'
        assert helper('revision') == 'b2'
        print('PASS: before/snapshot/after/restore recovers data and revision; source remains unchanged', flush=True)
        restore(good, 'recovery_verified', success=False)
        assert sql('SELECT value FROM recovery_probe WHERE id=1;', 'recovery_verified') == 'before'
        print('PASS: existing database is never reused', flush=True)
        restore(error, 'recovery_sql_error', success=False)
        assert sql("SELECT to_regclass('public.recovery_probe') IS NULL;", 'recovery_sql_error') == 't'
        print('PASS: injected SQL error returns nonzero and rolls back restore transaction', flush=True)
        invalid_checks = directory / 'invalid-verify.sql'
        invalid_checks.write_text("DO $$ BEGIN RAISE EXCEPTION 'incorrect expected data'; END $$;")
        restore(good, 'recovery_bad_data', success=False, verify=invalid_checks)
        mismatch = snapshot('wrong-revision', revision='wrong_revision')
        restore(mismatch, 'recovery_bad_revision', success=False)
        with good.open('ab') as stream:
            stream.write(b'tampered')
        restore(good, 'recovery_bad_checksum', success=False)
        assert sql("SELECT count(*) FROM pg_database WHERE datname='recovery_bad_checksum';") == '0'
        assert sql('SELECT value FROM recovery_probe WHERE id=1;') == 'after'
        assert helper('revision') == 'b2'
        for name in ('backend', 'worker'):
            container = compose('ps', '-aq', name, text=True).strip()
            assert run('docker', 'inspect', '--format', '{{.State.Running}}', container, text=True).strip() == 'false'
        print('PASS: data/revision/checksum failures cannot activate recovery or restart writers', flush=True)
    finally:
        subprocess.run(['docker', 'compose', 'down', '--volumes', '--remove-orphans'], cwd=directory, env=environment, check=False)
