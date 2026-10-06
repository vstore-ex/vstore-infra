#!/usr/bin/env python3
"""Encrypted off-host recovery bundles; restoration never activates production."""
import argparse
from datetime import datetime, timezone
import gzip
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from urllib.parse import urlsplit


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


recovery = module('database_recovery', 'database-recovery.py')
release = recovery.release
permissions = module('deployment_permissions', 'secure-deployment.py')
TAG = 'vstore-a06'
PENDING_TAG = 'vstore-a06-pending'
RESUME = Path('backups/.offhost-resume.json')
PAYLOADS = ('database.sql.gz', 'database.sql.gz.metadata.json', 'database-globals.sql.gz', 'infra.tar.gz',
            'environment', 'release.json', 'storage.tar.gz', 'uploads.tar.gz',
            'redis.tar.gz', 'certificates.tar.gz', 'webroot.tar.gz', 'media.json')


def run(*command, **kwargs):
    # Error output from cloud clients can include URLs/SAS or connection strings.
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=3600, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{Path(command[0]).name} failed (exit {result.returncode}); secret-bearing output suppressed')
    return result.stdout


def private_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'r') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('Backup configuration/password/SAS files must be owned by this user, regular, unlinked and 0600')
        return stream.read()


def configuration(path, test_local=False):
    value = json.loads(private_file(path))
    if not re.fullmatch(r'[a-z0-9-]{1,63}', value['host']):
        raise ValueError('Invalid backup host identifier')
    for field in ('interval_hours', 'keep_weekly'):
        if type(value[field]) is not int or value[field] < 1:
            raise ValueError('Backup interval and retention must be positive integers')
    repo = value['repository']
    if not isinstance(repo, str) or not re.fullmatch(r'azure:[a-z0-9-]{3,63}:/[A-Za-z0-9/_-]*', repo):
        if not test_local or not Path(repo).is_absolute():
            raise ValueError('Production backups require a dedicated Azure repository; local repositories are test-only')
    password = Path(value['password_file'])
    if not password.is_absolute() or not private_file(password).strip():
        raise ValueError('An absolute, nonempty private password file is required')
    # Do not inherit account keys, arbitrary restic passwords or alternate endpoints.
    environment = {key: val for key, val in os.environ.items()
                   if not key.startswith(('RESTIC_', 'AZURE_'))}
    environment.update(RESTIC_REPOSITORY=repo, RESTIC_PASSWORD_FILE=str(password), TZ='UTC')
    if repo.startswith('azure:'):
        if not re.fullmatch(r'[a-z0-9]{3,24}', value['azure_account_name']):
            raise ValueError('Invalid Azure account name')
        sas = private_file(value['azure_sas_file']).strip().removeprefix('?')
        if not sas or '\n' in sas:
            raise ValueError('A container-scoped private SAS file is required')
        environment.update(AZURE_ACCOUNT_NAME=value['azure_account_name'], AZURE_ACCOUNT_SAS=sas)
    value['environment'] = environment
    return value


def restic(config, *args, cwd=None):
    return run('restic', *args, env=config['environment'], cwd=cwd)


def snapshots(config):
    return json.loads(restic(config, 'snapshots', '--json', '--host', config['host'], '--tag', TAG)) or []


def publish(config, bundle, point):
    # restic can save a partial snapshot and exit 3. Such snapshots never carry
    # the completed tag and cannot postpone the next boot's retry.
    output = restic(config, 'backup', '--json', '--host', config['host'], '--tag', PENDING_TAG,
                    '--time', datetime.fromisoformat(point).strftime('%Y-%m-%d %H:%M:%S'),
                    'bundle', cwd=bundle.parent)
    summary = [json.loads(line) for line in output.splitlines() if line.strip()]
    snapshot = next(item['snapshot_id'] for item in summary if item.get('message_type') == 'summary')
    restic(config, 'tag', '--add', TAG, '--remove', PENDING_TAG, snapshot)
    completed = [item for item in snapshots(config) if item.get('original', item['id']) == snapshot]
    if len(completed) != 1:
        raise ValueError('Completed off-host snapshot publication could not be verified')
    return completed[0]['id']


def age_hours(items, now=None):
    if not items:
        return None
    now = now or datetime.now(timezone.utc)
    dates = [datetime.fromisoformat(item['time'].replace('Z', '+00:00')) for item in items]
    if any(date.tzinfo is None for date in dates):
        raise ValueError('Snapshot timestamps must include timezone')
    age = (now - max(dates)).total_seconds() / 3600
    if age < -5 / 60:
        raise ValueError('Future snapshot timestamp; check host clock before declaring protection')
    return max(0, age)


def compose_config():
    return json.loads(run('docker', 'compose', 'config', '--format', 'json'))


def validate_topology(resolved):
    services = resolved['services']
    required = {'postgres': {'/var/lib/postgresql/data'}, 'redis': {'/data'},
                'backend': {'/app/storage', '/tmp/uploads'}, 'worker': {'/app/storage', '/tmp/uploads'},
                'proxy': {'/etc/letsencrypt', '/var/www/certbot'}}
    for service, targets in required.items():
        writable = {item['target'] for item in services[service].get('volumes', []) if not item.get('read_only', False)}
        if writable != targets:
            raise ValueError('Review changed writable storage topology before claiming backup coverage')
    environment = services['backend']['environment']
    for key in ('CELERY_BROKER_URL', 'CELERY_RESULT_BACKEND'):
        url = urlsplit(environment.get(key, 'redis://redis:6379/0'))
        if url.scheme != 'redis' or url.hostname != 'redis' or url.port not in (None, 6379):
            raise ValueError('External broker/result storage requires a separate reviewed backup strategy')


def running(service):
    return run('docker', 'compose', 'ps', '-q', service, text=True).strip()


def archive(command, path):
    with path.open('xb') as output, gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as compressed:
        # Stream large dumps/media without buffering them into host RAM.
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
            try:
                with process.stdout:
                    shutil.copyfileobj(process.stdout, compressed)
                if process.wait(timeout=1200):
                    raise RuntimeError('Snapshot export failed; output suppressed')
            except BaseException:
                process.kill()
                process.wait()
                raise
    path.chmod(0o600)


def tar_command(image, source, bind=False):
    command = ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
               '--cap-add', 'DAC_OVERRIDE', '--security-opt', 'no-new-privileges', '--entrypoint', 'tar']
    if bind:
        command += ['--mount', f'type=bind,src={source},dst=/state,readonly', image, '-cf', '-', '-C', '/state', '.']
    else:
        container, path = source
        command += ['--volumes-from', container + ':ro', image, '-cf', '-', '-C', path, '.']
    return command


def media_export(connection, bundle):
    records = []
    if connection:
        from azure.core import MatchConditions
        from azure.storage.blob import ContainerClient
        with ContainerClient.from_connection_string(connection, 'media') as client:
            initial = list(client.list_blobs(include=['metadata']))
            (bundle / 'media').mkdir(mode=0o700)
            for blob in initial:
                if str(blob.blob_type) not in ('BlobType.BLOCKBLOB', 'BlockBlob'):
                    raise ValueError('Media export supports block blobs only')
                # Blob names are opaque, potentially hostile paths. Store by hash.
                import hashlib
                filename = hashlib.sha256(blob.name.encode()).hexdigest()
                destination = bundle / 'media' / filename
                with destination.open('xb') as stream:
                    client.download_blob(blob.name, etag=blob.etag,
                                         match_condition=MatchConditions.IfNotModified).readinto(stream)
                settings = blob.content_settings
                records.append({'name': blob.name, 'file': filename, 'sha256': recovery.digest(destination),
                                'size': blob.size, 'metadata': blob.metadata,
                                'content_settings': {key: getattr(settings, key) for key in
                                    ('content_type', 'content_encoding', 'content_language',
                                     'content_disposition', 'cache_control')}})
            final = list(client.list_blobs())
            identity = lambda blobs: sorted((item.name, item.etag, item.size) for item in blobs)
            if identity(initial) != identity(final):
                raise ValueError('Media changed during export; refusing an inconsistent recovery point')
    (bundle / 'media.json').write_text(json.dumps({'configured': bool(connection), 'blobs': records}, indent=2))


def validate_tar(path):
    with tarfile.open(path, 'r:gz') as archive_file:
        members = archive_file.getmembers()
        links = {PurePosixPath(item.name) for item in members if item.issym() or item.islnk()}
        for item in members:
            parts = PurePosixPath(item.name).parts
            if item.name.startswith('/') or '..' in parts or not (item.isfile() or item.isdir() or item.issym() or item.islnk()):
                raise ValueError('Recovery archive contains unsafe paths or special files')
            if any(parent in links for parent in PurePosixPath(item.name).parents):
                raise ValueError('Recovery archive writes through a link')
            if item.issym() or item.islnk():
                base = PurePosixPath(item.name).parent if item.issym() else PurePosixPath('.')
                path_parts = list(base.parts)
                if item.linkname.startswith('/'):
                    raise ValueError('Recovery archive contains an absolute link')
                for part in PurePosixPath(item.linkname).parts:
                    if part == '..':
                        if not path_parts:
                            raise ValueError('Recovery archive link escapes the recovery directory')
                        path_parts.pop()
                    elif part != '.':
                        path_parts.append(part)
                    if PurePosixPath(*path_parts) in links:
                        raise ValueError('Recovery archive contains a link chain')
                target = PurePosixPath(*path_parts)
                if target in links or any(parent in links for parent in target.parents):
                    raise ValueError('Recovery archive contains a link chain')


def verify_bundle(bundle):
    if bundle.is_symlink() or any(path.is_symlink() for path in bundle.rglob('*')):
        raise ValueError('Recovery bundle must not contain filesystem links')
    manifest = json.loads((bundle / 'manifest.json').read_text())
    files = manifest['sha256']
    if manifest['version'] != 1 or not set(PAYLOADS).issubset(files):
        raise ValueError('Incomplete recovery bundle manifest')
    actual = {path.relative_to(bundle).as_posix() for path in bundle.rglob('*') if path.is_file()}
    if actual != set(files) | {'manifest.json'}:
        raise ValueError('Unexpected or missing recovery bundle files')
    for name, checksum in files.items():
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('Unsafe recovery manifest path')
        source = bundle / name
        if source.is_symlink() or recovery.digest(source) != checksum:
            raise ValueError('Recovery bundle checksum mismatch')
        if name.endswith('.tar.gz'):
            validate_tar(source)
    return manifest


def remember_running(containers):
    if any(not re.fullmatch(r'[0-9a-f]{12,64}', container) for container in containers.values()):
        raise ValueError('Cannot journal a missing or ambiguous running container')
    value = {'version': 1, 'containers': containers}
    fd, temporary = tempfile.mkstemp(prefix='.offhost-resume-', dir=RESUME.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if RESUME.exists():
            raise ValueError('An interrupted backup must resume before another capture')
        os.replace(temporary, RESUME)
        sync_directory(RESUME.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync_directory(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def resume_pending():
    if not RESUME.exists():
        return
    value = json.loads(private_file(RESUME))
    if value['version'] != 1 or set(value['containers']) - {'redis', 'backend', 'worker', 'certbot'}:
        raise ValueError('Invalid interrupted backup resume record')
    for service, container in value['containers'].items():
        actual = run('docker', 'compose', 'ps', '-aq', service, text=True).strip()
        if actual != container or not re.fullmatch(r'[0-9a-f]{12,64}', container):
            raise ValueError('Container changed after interrupted backup; manual recovery required')
    services = list(value['containers'])
    if 'redis' in services:
        run('docker', 'compose', 'start', 'redis')
        deadline = time.monotonic() + 60
        while True:
            try:
                if run('docker', 'compose', 'exec', '-T', 'redis', 'redis-cli', 'PING', text=True).strip() == 'PONG':
                    break
            except RuntimeError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError('Redis did not resume; writers remain stopped')
            time.sleep(1)
    remaining = [service for service in services if service != 'redis']
    if remaining:
        run('docker', 'compose', 'start', *remaining)
    current, _ = release.load()
    deadline = time.monotonic() + 90
    while True:
        try:
            release.verify(current)
            break
        except ValueError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)
    RESUME.unlink()
    sync_directory(RESUME.parent)
    print('PASS: interrupted backup services resumed with existing containers/images')


def wait_release(current):
    deadline = time.monotonic() + 180
    while True:
        try:
            release.verify(current)
            return
        except (ValueError, subprocess.CalledProcessError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(3)


def backup(config, if_due=False):
    # Nothing except the root/lock may be traversed before acquiring shared flock.
    permissions.secure(Path.cwd(), lock_only=True)
    with release.locked(None, allow_backup_resume=True):
        permissions.secure(Path.cwd())
        resume_pending()  # Resume before any off-host network operation, even offline.
        items = snapshots(config)  # Also proves authentication/decryption before downtime.
        age = age_hours(items)
        if if_due and age is not None and age < config['interval_hours']:
            print(f'PASS: off-host recovery point age {age:.2f} hours; not due')
            return
        if config.get('allow_quiesce') is not True:
            raise ValueError('Backup requires an approved writer maintenance window (allow_quiesce=true)')
        current, _ = release.load()
        if set(current) != set(release.GROUPS):
            raise ValueError('Adopt every application and edge digest before backup')
        wait_release(current)  # Boot can precede container health; no writers stopped yet.
        if run('git', 'status', '--porcelain', '--untracked-files=no', text=True).strip():
            raise ValueError('Tracked infrastructure files must match the committed recovery configuration')
        resolved = compose_config()
        validate_topology(resolved)
        pg_image = release.running_image('postgres')
        redis_image = release.running_image('redis')
        if run('docker', 'compose', 'exec', '-T', 'redis', 'redis-cli', '--raw', 'CONFIG', 'GET', 'dir', text=True).splitlines() != ['dir', '/data']:
            raise ValueError('Redis persistence must remain in the captured /data mount')
        containers = {service: running(service) for service in ('backend', 'worker', 'redis', 'proxy', 'certbot')}
        if (not all(containers[service] for service in ('backend', 'worker', 'redis', 'proxy'))
                or not Path('storage').is_dir() or Path('storage').is_symlink()):
            raise ValueError('Required application containers/storage are missing')
        restart = {service: containers[service] for service in ('redis', 'backend', 'worker', 'certbot') if containers[service]}
        with tempfile.TemporaryDirectory(prefix='offhost-', dir='backups') as staging:
            bundle = Path(staging).resolve() / 'bundle'
            bundle.mkdir(mode=0o700)
            started = time.monotonic()
            remember_running(restart)
            try:
                run('docker', 'compose', 'stop', '--timeout', '120', 'backend', 'worker', 'certbot')
                recovery.writers_stopped()
                # Force an up-to-date RDB after stopping producers/consumers. Then
                # stop Redis so its full persistence directory is coherent.
                if run('docker', 'compose', 'exec', '-T', 'redis', 'redis-cli', 'SAVE', text=True).strip() != 'OK':
                    raise ValueError('Redis snapshot failed')
                run('docker', 'compose', 'stop', '--timeout', '120', 'redis')
                point = datetime.now(timezone.utc).isoformat()
                database = recovery.source_database()
                revision = recovery.revision()
                command = ['docker', 'compose', 'exec', '-T', 'postgres', 'sh', '-c',
                           'exec pg_dump -U "$POSTGRES_USER" -d "$1"', 'backup-dump', database]
                archive(command, bundle / 'database.sql.gz')
                recovery.record(bundle / 'database.sql.gz', revision, current['BACKEND_IMAGE'])
                archive(['docker', 'compose', 'exec', '-T', 'postgres', 'sh', '-c',
                         'exec pg_dumpall -U "$POSTGRES_USER" --globals-only'], bundle / 'database-globals.sql.gz')
                archive(['git', 'archive', '--format=tar', 'HEAD'], bundle / 'infra.tar.gz')
                shutil.copyfile('.env', bundle / 'environment')
                shutil.copyfile('docker-compose.override.yml', bundle / 'release.json')
                sources = {'storage': (str(Path('storage').resolve()), True),
                           'uploads': ((containers['backend'], '/tmp/uploads'), False),
                           'redis': ((containers['redis'], '/data'), False),
                           'certificates': ((containers['proxy'], '/etc/letsencrypt'), False),
                           'webroot': ((containers['proxy'], '/var/www/certbot'), False)}
                for name, (source, bind) in sources.items():
                    archive(tar_command(pg_image, source, bind), bundle / (name + '.tar.gz'))
                media_export(resolved['services']['backend']['environment'].get('AZURE_STORAGE_CONNECTION_STRING', ''), bundle)
                manifest = {'version': 1, 'recovery_point': point,
                            'infra_commit': run('git', 'rev-parse', 'HEAD', text=True).strip(),
                            'postgres_image': pg_image, 'redis_image': redis_image,
                            'database': database, 'revision': revision,
                            'policy': {'interval_hours': config['interval_hours'], 'keep_weekly': config['keep_weekly'],
                                       'rto_hours': None},
                            'sha256': {path.relative_to(bundle).as_posix(): recovery.digest(path)
                                       for path in bundle.rglob('*') if path.is_file()}}
                (bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
                verify_bundle(bundle)
            finally:
                # Restart only services that were running. Never recreate an image,
                # apply migrations or mask a failed resume as successful backup.
                resume_pending()
            manifest['quiesce_seconds'] = round(time.monotonic() - started, 3)
            (bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
            # Stable relative path avoids keeping separate retention groups per run.
            snapshot = publish(config, bundle, point)
            # backup exits 3 on unreadable files; never accept a partial snapshot.
            print(json.dumps({'snapshot_id': snapshot, 'recovery_point': point,
                              'quiesce_seconds': manifest['quiesce_seconds'], 'offhost': True}))


def restore_bundle(config, snapshot, target):
    if not re.fullmatch(r'[0-9a-f]{64}', snapshot):
        raise ValueError('Restore requires a full explicit snapshot ID; latest is not accepted')
    if snapshot not in {item['id'] for item in snapshots(config)}:
        raise ValueError('Snapshot does not belong to this host and backup tag')
    target.mkdir(mode=0o700)  # Refuse existing targets, including production directories.
    started = time.monotonic()
    restic(config, 'restore', snapshot, '--verify', '--target', str(target.resolve()))
    manifest = verify_bundle(target / 'bundle')
    print(json.dumps({'snapshot_id': snapshot, 'recovery_point': manifest['recovery_point'],
                      'download_verify_seconds': round(time.monotonic() - started, 3),
                      'activated': False}))


def restore_media(bundle, target, connection_file):
    verify_bundle(bundle)
    if not re.fullmatch(r'recovery-[a-z0-9-]{1,50}', target):
        raise ValueError('Media target must be a new recovery-* container')
    from azure.storage.blob import ContainerClient, ContentSettings
    media = json.loads((bundle / 'media.json').read_text())
    if not media['configured']:
        raise ValueError('This recovery point has no Azure media container')
    for blob in media['blobs']:
        if not re.fullmatch(r'[0-9a-f]{64}', blob['file']) or not isinstance(blob['name'], str):
            raise ValueError('Invalid media recovery manifest')
    with ContainerClient.from_connection_string(private_file(connection_file).strip(), target) as client:
        client.create_container()  # Fails if existing; never overwrites production media.
        for blob in media['blobs']:
            path = bundle / 'media' / blob['file']
            if path.stat().st_size != blob['size'] or recovery.digest(path) != blob['sha256']:
                raise ValueError('Media metadata/checksum mismatch')
            with path.open('rb') as stream:
                client.upload_blob(blob['name'], stream, metadata=blob['metadata'],
                                   content_settings=ContentSettings(**blob['content_settings']), overwrite=False)
            import hashlib
            checksum = hashlib.sha256()
            for chunk in client.download_blob(blob['name']).chunks():
                checksum.update(chunk)
            if checksum.hexdigest() != blob['sha256']:
                raise ValueError('Restored Azure media checksum mismatch')
    print(f'PASS: restored {len(media["blobs"])} blobs into a new private container; not activated')


def restore_storage(bundle, kind, target, volume=False):
    manifest = verify_bundle(bundle)
    source = (bundle / (kind + '.tar.gz')).resolve()
    release.immutable(manifest['postgres_image'])
    image = manifest['postgres_image']
    if volume:
        if not re.fullmatch(r'recovery-[a-z0-9-]{1,50}_[a-z_]{1,32}', target):
            raise ValueError('Volume must belong to a new recovery-* project')
        existing = subprocess.run(['docker', 'volume', 'inspect', target], capture_output=True)
        if existing.returncode == 0:
            raise ValueError('Existing recovery volumes must never be overwritten')
        nonce = uuid.uuid4().hex
        run('docker', 'volume', 'create', '--label', 'vstore.recovery=true',
            '--label', 'vstore.recovery.token=' + nonce, target)
        labels = json.loads(run('docker', 'volume', 'inspect', '--format', '{{json .Labels}}', target))
        if labels.get('vstore.recovery.token') != nonce:
            raise ValueError('Recovery volume was created concurrently; refusing to write into it')
        mount = f'type=volume,src={target},dst=/state'
    else:
        destination = Path(target)
        destination.mkdir(mode=0o700)  # Refuse existing paths, even empty ones.
        mount = f'type=bind,src={destination.resolve()},dst=/state'
    run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--security-opt', 'no-new-privileges',
        '--mount', mount, '--mount', f'type=bind,src={source},dst=/snapshot.tar.gz,readonly',
        '--entrypoint', 'tar', image, '-xzf', '/snapshot.tar.gz', '-C', '/state')
    print('PASS: state restored into a new recovery target; not activated')


def main():
    os.umask(0o077)
    def interrupted(signum, frame):
        raise InterruptedError('Backup interrupted; resume record retained if service restart fails')
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--test-local-repository', action='store_true', help='Disposable drill only; never use on production')
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('backup')
    create.add_argument('--if-due', action='store_true')
    commands.add_parser('status')
    commands.add_parser('resume')
    retention = commands.add_parser('retention')
    retention.add_argument('--apply', action='store_true', help='Delete old snapshots according to reviewed policy')
    restore = commands.add_parser('restore')
    restore.add_argument('--snapshot', required=True)
    restore.add_argument('--target', type=Path, required=True)
    media = commands.add_parser('restore-media')
    media.add_argument('--bundle', type=Path, required=True)
    media.add_argument('--target', required=True)
    media.add_argument('--connection-file', type=Path, required=True)
    state = commands.add_parser('restore-storage')
    state.add_argument('--bundle', type=Path, required=True)
    state.add_argument('--kind', choices=('storage', 'uploads', 'redis', 'certificates', 'webroot'), required=True)
    state.add_argument('--target', required=True)
    state.add_argument('--volume', action='store_true')
    args = parser.parse_args()
    if args.command == 'restore-media':
        restore_media(args.bundle, args.target, args.connection_file)
        return
    if args.command == 'restore-storage':
        restore_storage(args.bundle, args.kind, args.target, args.volume)
        return
    if args.command == 'resume':
        permissions.secure(Path.cwd(), lock_only=True)
        with release.locked(None, allow_backup_resume=True):
            permissions.secure(Path.cwd())
            resume_pending()
        return
    if args.config is None:
        raise ValueError('A private backup configuration is required')
    config = configuration(args.config, args.test_local_repository)
    if args.command == 'backup':
        backup(config, args.if_due)
    elif args.command == 'restore':
        restore_bundle(config, args.snapshot, args.target)
    elif args.command == 'status':
        age = age_hours(snapshots(config))
        print(json.dumps({'recovery_point_age_hours': age, 'interval_hours': config['interval_hours'],
                          'overdue': age is None or age >= config['interval_hours']}))
        if age is None or age >= config['interval_hours']:
            sys.exit(2)
    else:
        command = ['forget', '--host', config['host'], '--tag', TAG, '--group-by', 'host,tags',
                   '--keep-weekly', str(config['keep_weekly']), '--keep-last', '2']
        command += ['--prune'] if args.apply else ['--dry-run']
        # Never prune automatically after backup. Keep recovery/password copies
        # off VM and run deletion with a separately approved maintenance identity.
        print(restic(config, *command).decode())


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never echo Azure SDK exceptions: they may contain secret-bearing URLs.
        print(f'ERROR: off-host operation failed ({type(error).__name__}); production restore was not activated', file=sys.stderr)
        sys.exit(1)
