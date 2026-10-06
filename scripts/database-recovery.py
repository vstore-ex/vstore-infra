#!/usr/bin/env python3
"""Manual recovery into a new database; never replace or activate the source."""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

spec = importlib.util.spec_from_file_location('release_state', Path(__file__).with_name('release-state.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def compose(*args, **kwargs):
    return subprocess.run(['docker', 'compose', *args], check=True, **kwargs)


def psql(sql=None, database=None, stream=None, transaction=False):
    # Resolve credentials inside the existing PostgreSQL container. -X excludes
    # psql startup output; SQL errors must stop scripts with a nonzero exit code.
    command = ['exec', '-T', 'postgres', 'sh', '-c',
               'db="${1:-$POSTGRES_DB}"; shift; exec psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$db" "$@"']
    command += ['recovery-psql', database or '']
    if transaction:
        command += ['--single-transaction', '-f', '-']
    result = compose(*command, input=sql, stdin=stream, stdout=subprocess.PIPE, text=stream is None)
    return result.stdout.strip() if stream is None else result.stdout.decode().strip()


def revisions(value):
    values = value.splitlines()
    if any(not re.fullmatch(r'[A-Za-z0-9_]{1,128}', item) for item in values):
        raise ValueError('Invalid Alembic revision rows; refusing a noisy or ambiguous value')
    if len(set(values)) != len(values):
        raise ValueError('Duplicate Alembic revision rows')
    return ','.join(sorted(values)) or 'base'


def revision(database=None):
    exists = psql("SELECT to_regclass('public.alembic_version') IS NOT NULL;", database)
    if exists == 'f':
        return 'base'
    if exists != 't':
        raise ValueError('Unexpected Alembic table discovery response')
    return revisions(psql('SELECT version_num FROM public.alembic_version ORDER BY version_num;', database))


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def record(path, expected, image=None):
    if image:
        release.immutable(image)
    if expected != revisions(expected.replace(',', '\n') if expected != 'base' else ''):
        raise ValueError('Expected revision must be canonical sorted IDs or base')
    with gzip.open(path, 'rb') as stream:
        while stream.read(1024 * 1024):
            pass  # Check CRC/truncation before publishing the sidecar.
    metadata = {'version': 1, 'sha256': digest(path), 'revision': expected,
                'database': psql('SELECT current_database();'),
                'postgres_major': int(psql('SHOW server_version_num;')) // 10000,
                'backend_image': image or ''}
    sidecar = Path(str(path) + '.metadata.json')
    # Do not replace evidence associated with an existing snapshot.
    with open(sidecar, 'x', opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        json.dump(metadata, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def writers_stopped():
    for service in ('backend', 'worker'):
        result = compose('ps', '-aq', service, stdout=subprocess.PIPE, text=True)
        for container in result.stdout.splitlines():
            state = subprocess.check_output(['docker', 'inspect', '--format', '{{.State.Running}}', container], text=True).strip()
            if state != 'false':
                raise ValueError(f'{service} is still running; stop all writers first')


def restore(path, target, verification):
    if not re.fullmatch(r'recovery_[a-z0-9_]{1,54}', target):
        raise ValueError('Target must be a new recovery_* database (max 63 characters)')
    metadata = json.loads(Path(str(path) + '.metadata.json').read_text())
    if metadata.get('version') != 1 or metadata.get('sha256') != digest(path):
        raise ValueError('Snapshot metadata/checksum mismatch')
    if metadata['revision'] != revisions(metadata['revision'].replace(',', '\n') if metadata['revision'] != 'base' else ''):
        raise ValueError('Invalid snapshot revision metadata')
    if int(psql('SHOW server_version_num;')) // 10000 != metadata['postgres_major']:
        raise ValueError('Recovery requires the recorded PostgreSQL major version')
    checks = verification.read_text().strip()
    if not checks:
        raise ValueError('Reviewed data verification SQL is required')
    writers_stopped()
    with tempfile.TemporaryFile() as sql:
        with gzip.open(path, 'rb') as compressed:
            shutil.copyfileobj(compressed, sql)
        sql.seek(0)
        # CREATE fails if the target already exists, even if empty. No DROP,
        # truncation, target reuse, activation or writer restart is automatic.
        psql(f'CREATE DATABASE "{target}" TEMPLATE template0;', 'postgres')
        try:
            psql(database=target, stream=sql, transaction=True)
            actual = revision(target)
            if actual != metadata['revision']:
                raise ValueError(f'Restored revision mismatch: expected {metadata["revision"]}, got {actual}')
            psql('BEGIN READ ONLY;\n' + checks + '\nROLLBACK;', target)
        except Exception:
            print(f'Recovery failed. Source untouched; writers remain stopped. Inspect {target}; do not activate it.', file=sys.stderr)
            raise
    print(f'PASS: restored and verified {target}, revision {actual}. Not activated; writers remain stopped.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock-fd', type=int)
    commands = parser.add_subparsers(dest='command', required=True)
    query = commands.add_parser('revision')
    query.add_argument('--database')
    metadata = commands.add_parser('record')
    metadata.add_argument('snapshot', type=Path)
    metadata.add_argument('--revision', required=True)
    metadata.add_argument('--image', help='Verified pre-deploy backend digest for compatibility review')
    recovery = commands.add_parser('restore')
    recovery.add_argument('snapshot', type=Path)
    recovery.add_argument('--target', required=True)
    recovery.add_argument('--verify-sql', type=Path, required=True)
    recovery.add_argument('--writers-stopped', required=True, action='store_true', help='Acknowledge maintenance mode and external writers are stopped')
    args = parser.parse_args()
    with release.locked(args.lock_fd):
        if args.command == 'revision':
            print(revision(args.database))
        elif args.command == 'record':
            record(args.snapshot, args.revision, args.image)
        else:
            restore(args.snapshot, args.target, args.verify_sql)


if __name__ == '__main__':
    try:
        main()
    except (OSError, EOFError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
