"""Lose a disposable host, recover PostgreSQL/Redis/files/media from encrypted restic."""
import base64
import gzip
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from azure.storage.blob import ContainerClient, ContentSettings

ROOT = Path(__file__).resolve().parents[1]
token = uuid.uuid4().hex[:10]
project = 'backup-smoke-' + token
replacement = 'recovery-' + token
emulator = 'azurite-backup-' + token
volumes = []


def execute(*args, **kwargs):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    if result.returncode:
        raise AssertionError(f'{args[:4]} failed: {result.stdout.decode()} {result.stderr.decode()}')
    return result.stdout


with tempfile.TemporaryDirectory(prefix='vstore-offhost-drill-') as temporary:
    root = Path(temporary)
    source = root / 'lost-host'
    source.mkdir(mode=0o700)
    escrow = root / 'offhost-escrow'
    escrow.mkdir(mode=0o700)
    password = escrow / 'password'
    password.write_text(secrets.token_hex(32))
    password.chmod(0o600)
    config = escrow / 'config.json'
    config.write_text(json.dumps({'host': project, 'repository': str(root / 'offhost-repository'),
                                 'password_file': str(password), 'interval_hours': 168,
                                 'keep_weekly': 4, 'allow_quiesce': True}))
    config.chmod(0o600)
    environment = {**os.environ, 'COMPOSE_PROJECT_NAME': project}
    settings = json.loads(config.read_text())
    restic_env = {**os.environ, 'RESTIC_REPOSITORY': settings['repository'], 'RESTIC_PASSWORD_FILE': str(password)}
    execute('restic', 'init', env=restic_env)
    execute('docker', 'pull', 'postgres:17')
    execute('docker', 'pull', 'redis:7.2-alpine')
    pg_digest = json.loads(execute('docker', 'image', 'inspect', '--format', '{{json .RepoDigests}}', 'postgres:17'))[0]

    def compose(*args, cwd=source, env=environment, **kwargs):
        return execute('docker', 'compose', *args, cwd=cwd, env=env, **kwargs)

    def helper(*args, cwd=source, success=True, configured=False, env=environment):
        # The fixture has no production secrets; expose exceptions here so CI
        # can diagnose a failed drill while the operator CLI suppresses them.
        command = [sys.executable, '-c',
                   "import runpy,sys; helper=runpy.run_path(sys.argv.pop(1),run_name='drill'); helper['main']()",
                   str(ROOT / 'scripts/offhost-backup.py')]
        if configured:
            command += ['--config', str(config), '--test-local-repository']
        result = subprocess.run(command + list(map(str, args)), cwd=cwd, env=env, capture_output=True)
        assert (result.returncode == 0) == success, result.stdout.decode() + result.stderr.decode()
        return result.stdout.decode()

    def sql(query, cwd=source, env=environment, database='live_app'):
        return compose('exec', '-T', 'postgres', 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1',
                       '-U', 'postgres', '-d', database, input=query.encode(), cwd=cwd, env=env).decode().strip()

    try:
        key = base64.b64encode(secrets.token_bytes(32)).decode()
        execute('docker', 'run', '-d', '--name', emulator, '-p', '127.0.0.1::10000',
                '-e', 'AZURITE_ACCOUNTS=fixture:' + key, 'mcr.microsoft.com/azure-storage/azurite:3.35.0',
                'azurite-blob', '--blobHost', '0.0.0.0', '--skipApiVersionCheck', '--silent', '--loose')
        port = json.loads(execute('docker', 'inspect', '--format', '{{json .NetworkSettings.Ports}}', emulator))['10000/tcp'][0]['HostPort']
        connection = f'DefaultEndpointsProtocol=http;AccountName=fixture;AccountKey={key};BlobEndpoint=http://127.0.0.1:{port}/fixture;'
        connection_file = escrow / 'media.connection'
        connection_file.write_text(connection)
        connection_file.chmod(0o600)
        media = ContainerClient.from_connection_string(connection, 'media')
        for attempt in range(30):
            try:
                media.create_container()
                break
            except Exception:
                if attempt == 29:
                    raise
                time.sleep(1)
        originals = {'nested/avatar.png': b'fixture avatar bytes', 'game/archive.zip': b'fixture game bytes'}
        for name, data in originals.items():
            media.upload_blob(name, data, metadata={'fixture': 'retained'}, content_settings=ContentSettings(content_type='application/octet-stream'))
        shutil.copytree(ROOT / 'scripts', source / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copyfile(ROOT / '.gitignore', source / '.gitignore')
        (source / '.env').write_text('DISPOSABLE_FIXTURE_ONLY=true\n')
        (source / 'storage').mkdir(mode=0o755)
        (source / 'storage/game.dat').write_bytes(b'local application bytes')
        sleeper = {'image': pg_digest, 'entrypoint': ['sh', '-c', 'exec sleep infinity'], 'init': True,
                   'stop_signal': 'SIGTERM',
                   'healthcheck': {'test': ['CMD', 'true'], 'interval': '1s', 'timeout': '2s', 'retries': 30}}
        fixture = {'services': {
            'postgres': {'image': 'postgres:17', 'environment': {'POSTGRES_USER': 'postgres', 'POSTGRES_DB': 'app',
                                                                'POSTGRES_PASSWORD': 'disposable-fixture-only'},
                         'volumes': ['postgres_data:/var/lib/postgresql/data'],
                         'healthcheck': {'test': ['CMD', 'pg_isready', '-U', 'postgres', '-d', 'app'],
                                         'interval': '1s', 'timeout': '2s', 'retries': 30}},
            'redis': {'image': 'redis:7.2-alpine', 'volumes': ['redis_data:/data'],
                      'healthcheck': {'test': ['CMD', 'redis-cli', 'ping'], 'interval': '1s', 'timeout': '2s', 'retries': 30}},
            'backend': {**sleeper, 'environment': {'DATABASE_URL': 'postgresql://postgres:disposable-fixture-only@postgres:5432/live_app',
                                                  'AZURE_STORAGE_CONNECTION_STRING': connection},
                        'volumes': ['./storage:/app/storage', 'uploads_temp:/tmp/uploads']},
            'worker': {**sleeper, 'volumes': ['./storage:/app/storage', 'uploads_temp:/tmp/uploads']},
            'frontend': sleeper, 'admin': sleeper,
            'proxy': {**sleeper, 'volumes': ['certbot_certs:/etc/letsencrypt', 'certbot_webroot:/var/www/certbot']},
            'certbot': {**sleeper, 'volumes': ['certbot_certs:/etc/letsencrypt']}},
            'volumes': {name: {} for name in ('postgres_data', 'redis_data', 'uploads_temp', 'certbot_certs', 'certbot_webroot')}}
        # The fixture connection is disposable and used only in this private temp repo.
        (source / 'docker-compose.yml').write_text(json.dumps(fixture))
        execute('git', 'init', '-b', 'test/backup', cwd=source)
        execute('git', '-c', 'user.name=Recovery Fixture', '-c', 'user.email=fixture@example.invalid',
                'add', 'scripts', '.gitignore', 'docker-compose.yml', cwd=source)
        execute('git', '-c', 'user.name=Recovery Fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '-m', 'test: add disposable recovery fixture', cwd=source)
        spec = importlib.util.spec_from_file_location('offhost_fixture', ROOT / 'scripts/offhost-backup.py')
        backup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backup)
        current = {name: pg_digest for name in backup.release.GROUPS}
        (source / 'docker-compose.override.yml').write_text(json.dumps(backup.release.document(current, dict.fromkeys(current, ''))))
        compose('up', '-d', '--wait', '--wait-timeout', '90')
        sql('CREATE DATABASE live_app TEMPLATE template0;', database='postgres')
        sql("CREATE TABLE recovery_probe(id integer PRIMARY KEY, value text NOT NULL); INSERT INTO recovery_probe VALUES (1, 'before');"
            "CREATE TABLE alembic_version(version_num varchar(32) PRIMARY KEY); INSERT INTO alembic_version VALUES ('a1');"
            "CREATE ROLE fixture_reader; GRANT SELECT ON recovery_probe TO fixture_reader;")
        compose('exec', '-T', 'redis', 'redis-cli', 'SET', 'auth:revoked:fixture', 'retained')
        compose('exec', '-T', 'backend', 'sh', '-c', 'echo partial-upload > /tmp/uploads/chunk; chown -R 10001:10001 /app/storage')
        compose('exec', '-T', 'proxy', 'sh', '-c', 'mkdir -p /etc/letsencrypt/archive/site /etc/letsencrypt/live/site; '
                'echo fixture-key > /etc/letsencrypt/archive/site/key.pem; chmod 600 /etc/letsencrypt/archive/site/key.pem; '
                'ln -s ../../archive/site/key.pem /etc/letsencrypt/live/site/key.pem')
        ids = compose('ps', '-aq').decode().splitlines()
        output = helper('backup', '--if-due', configured=True)
        snapshot = json.loads(output.splitlines()[-1])
        snapshot_id = snapshot['snapshot_id']
        assert len(snapshot_id) == 64
        assert not (source / 'backups/.offhost-resume.json').exists()
        assert compose('ps', '-aq').decode().splitlines() == ids
        assert 'not due' in helper('backup', '--if-due', configured=True)
        print('PASS: boot makes one completed encrypted backup, then skips within 168 hours', flush=True)
        # A resume record survives VM/process loss and resumes the exact containers.
        os.chdir(source)
        backup.remember_running({service: compose('ps', '-q', service).decode().strip()
                                 for service in ('redis', 'backend', 'worker', 'certbot')})
        compose('stop', 'backend', 'worker', 'redis', 'certbot')
        assert 'resumed' in helper('resume')
        assert compose('ps', '-aq').decode().splitlines() == ids
        os.chdir(ROOT)
        print('PASS: interrupted backup resumes unchanged containers before contacting off-host storage', flush=True)
        helper('retention', configured=True)
        execute('restic', 'check', '--read-data', env=restic_env)
        # Remove all source containers/volumes, local deployment state, and live media.
        compose('down', '--volumes', '--remove-orphans')
        media.delete_container()
        shutil.rmtree(source)
        assert not source.exists()
        began = time.monotonic()
        target = root / 'replacement-download'
        helper('restore', '--snapshot', snapshot_id, '--target', target, cwd=root, configured=True)
        bundle = target / 'bundle'
        manifest = backup.verify_bundle(bundle)
        new_host = root / 'replacement-host'
        new_host.mkdir(mode=0o700)
        with tarfile.open(bundle / 'infra.tar.gz') as archive:
            archive.extractall(new_host, filter='data')
        shutil.copyfile(bundle / 'environment', new_host / '.env')
        shutil.copyfile(bundle / 'release.json', new_host / 'docker-compose.override.yml')
        (new_host / '.env').chmod(0o600)
        (new_host / 'docker-compose.override.yml').chmod(0o600)
        helper('restore-storage', '--bundle', bundle, '--kind', 'storage', '--target', new_host / 'storage', cwd=root)
        for kind, name in (('redis', 'redis_data'), ('uploads', 'uploads_temp'),
                           ('certificates', 'certbot_certs'), ('webroot', 'certbot_webroot')):
            volume = replacement + '_' + name
            helper('restore-storage', '--bundle', bundle, '--kind', kind, '--target', volume, '--volume', cwd=root)
            volumes.append(volume)
        replacement_env = {**os.environ, 'COMPOSE_PROJECT_NAME': replacement}
        compose('up', '-d', '--wait', '--wait-timeout', '90', 'postgres', 'redis', cwd=new_host, env=replacement_env)
        # Review globals for this fixture: the freshly initialized postgres role
        # already exists; other owners/grants must be restored before the dump.
        with gzip.open(bundle / 'database-globals.sql.gz', 'rt') as stream:
            globals_sql = stream.read().replace('CREATE ROLE postgres;', '')
        assert 'CREATE ROLE fixture_reader;' in globals_sql
        sql(globals_sql, cwd=new_host, env=replacement_env, database='postgres')
        checks = new_host / 'verify.sql'
        checks.write_text("DO $$ BEGIN IF (SELECT value FROM recovery_probe WHERE id=1) IS DISTINCT FROM 'before' "
                          "OR (SELECT count(*) FROM recovery_probe) <> 1 THEN RAISE EXCEPTION 'wrong restored data'; END IF; END $$;")
        execute(sys.executable, str(new_host / 'scripts/database-recovery.py'), 'restore', str(bundle / 'database.sql.gz'),
                '--target', 'recovery_verified', '--verify-sql', str(checks), '--writers-stopped', cwd=new_host, env=replacement_env)
        assert sql('SELECT value FROM recovery_probe WHERE id=1;', cwd=new_host, env=replacement_env, database='recovery_verified') == 'before'
        assert sql('SELECT version_num FROM alembic_version;', cwd=new_host, env=replacement_env, database='recovery_verified') == 'a1'
        assert sql("SELECT has_table_privilege('fixture_reader', 'recovery_probe', 'SELECT');", cwd=new_host,
                   env=replacement_env, database='recovery_verified') == 't'
        assert compose('exec', '-T', 'redis', 'redis-cli', 'GET', 'auth:revoked:fixture', cwd=new_host, env=replacement_env).strip() == b'retained'
        assert (new_host / 'storage/game.dat').read_bytes() == b'local application bytes'
        for volume, path, value in ((replacement + '_uploads_temp', 'chunk', b'partial-upload'),
                                    (replacement + '_certbot_certs', 'live/site/key.pem', b'fixture-key')):
            assert execute('docker', 'run', '--rm', '--network', 'none', '-v', volume + ':/state:ro',
                           '--entrypoint', 'cat', pg_digest, '/state/' + path).strip() == value
        destination = 'recovery-' + token
        helper('restore-media', '--bundle', bundle, '--target', destination, '--connection-file', connection_file, cwd=root)
        with ContainerClient.from_connection_string(connection, destination) as client:
            assert len(list(client.list_blobs())) == len(originals)
            for name, data in originals.items():
                assert client.download_blob(name).readall() == data
                props = client.get_blob_client(name).get_blob_properties()
                assert props.metadata == {'fixture': 'retained'}
                assert props.content_settings.content_type == 'application/octet-stream'
            assert client.get_container_properties().public_access is None
        helper('restore-media', '--bundle', bundle, '--target', destination, '--connection-file', connection_file, cwd=root, success=False)
        helper('restore-storage', '--bundle', bundle, '--kind', 'redis', '--target', volumes[0], '--volume', cwd=root, success=False)
        elapsed = round(time.monotonic() - began, 3)
        report = {'restore_seconds': elapsed, 'recovery_point': manifest['recovery_point'],
                  'quiesce_seconds': manifest['quiesce_seconds'], 'revision': 'a1',
                  'verified': ['PostgreSQL data', 'PostgreSQL role grants', 'Alembic revision', 'Redis auth state', 'local media',
                               'upload chunks', 'certificate symlinks', 'Azure Blob bytes/metadata/private access'],
                  'source_host_destroyed': True, 'production_touched': False,
                  'repository_backend': 'isolated local restic fixture; Azure upload requires adoption validation'}
        print(json.dumps(report, indent=2), flush=True)
        if os.environ.get('A06_DRILL_REPORT'):
            Path(os.environ['A06_DRILL_REPORT']).write_text(json.dumps(report, indent=2) + '\n')
        compose('down', '--volumes', '--remove-orphans', cwd=new_host, env=replacement_env)
    finally:
        os.chdir(ROOT)
        if source.exists():
            subprocess.run(['docker', 'compose', 'down', '--volumes', '--remove-orphans'], cwd=source, env=environment, capture_output=True)
        if 'new_host' in locals() and new_host.exists():
            subprocess.run(['docker', 'compose', 'down', '--volumes', '--remove-orphans'], cwd=new_host,
                           env={**os.environ, 'COMPOSE_PROJECT_NAME': replacement}, capture_output=True)
        for volume in volumes:
            subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True)
        subprocess.run(['docker', 'rm', '-f', emulator], capture_output=True)
