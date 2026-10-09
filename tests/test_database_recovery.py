import importlib.util
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('database_recovery', ROOT / 'scripts/database-recovery.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def source_config(self, url):
        return subprocess.CompletedProcess([], 0, stdout=json.dumps({
            'services': {'backend': {'environment': {'DATABASE_URL': url}}}}))

    def test_source_database_follows_application_url(self):
        for scheme in ('postgresql', 'postgresql+psycopg2', 'postgres'):
            with patch.object(recovery, 'compose', return_value=self.source_config(
                    f'{scheme}://user:secret@postgres:5432/recovery_live')):
                self.assertEqual(recovery.source_database(), 'recovery_live')
        with patch.object(recovery, 'source_database', return_value='recovery_live'), \
                patch.object(recovery, 'compose', return_value=subprocess.CompletedProcess([], 0, stdout='t')) as compose:
            recovery.psql('SELECT true;')
        self.assertEqual(compose.call_args.args[-1], 'recovery_live')

    def test_source_database_rejects_other_servers_and_connection_overrides(self):
        for url in ('postgresql://user:secret@external/app', 'postgresql://user:secret@postgres:5433/app',
                    'postgresql://user:secret@postgres/app?host=external', 'sqlite:///app',
                    'postgresql://user:secret@postgres/', 'postgresql://user:secret@postgres/a%0ab',
                    'postgresql://user:secret@postgres/app%5Farchive',
                    'postgresql://user:secret@postgres/app\n',
                    'postgresql://user:secret@postgres/ap\tp',
                    'postgresql://user:secret@postgres:bad/app'):
            with self.subTest(url=url), patch.object(recovery, 'compose', return_value=self.source_config(url)), \
                    self.assertRaises(ValueError) as error:
                recovery.source_database()
            self.assertNotIn('secret', str(error.exception))

    def test_snapshot_checksum_uses_streaming_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'snapshot'
            payload = b'snapshot-data' * 100000
            path.write_bytes(payload)
            self.assertEqual(recovery.digest(path), hashlib.sha256(payload).hexdigest())

    def test_revision_has_no_entrypoint_output(self):
        with patch.object(recovery, 'psql', side_effect=['t', 'z9\na1']):
            self.assertEqual(recovery.revision(), 'a1,z9')
        for value in ('[entrypoint] skipping migrations\na1', 'a1 (head)', 'a1\na1', 'bad;sql'):
            with self.assertRaises(ValueError):
                recovery.revisions(value)

    def test_uninitialized_database_is_base(self):
        with patch.object(recovery, 'psql', return_value='f'):
            self.assertEqual(recovery.revision(), 'base')

    def test_target_names_cannot_select_source_or_inject_sql(self):
        for target in ('app', 'postgres', 'template0', 'recovery_x;DROP', 'recovery_' + 'x' * 55):
            with self.assertRaises(ValueError):
                recovery.restore(Path('unused'), target, Path('unused'))

    def test_workflow_has_no_nonempty_restore_or_noisy_revision_hint(self):
        workflow = (ROOT / '.github/workflows/deploy-service.yml').read_text()
        self.assertNotIn('backend alembic current', workflow)
        self.assertNotIn('gunzip -c', workflow)
        self.assertIn('database-recovery.py --lock-fd 200 revision', workflow)
        self.assertIn('database-recovery.py --lock-fd 200 record', workflow)
        self.assertNotIn('database-recovery.py restore', workflow)

    def test_workflow_migration_cleanup_scopes_to_project_and_avoids_global_filter(self):
        workflow = (ROOT / '.github/workflows/deploy-service.yml').read_text()
        self.assertNotIn('docker ps -qa --filter "name=backend-run"', workflow)
        self.assertIn('filter "label=com.docker.compose.project=$project"', workflow)
        self.assertIn('filter "label=com.docker.compose.service=backend"', workflow)
        self.assertIn('filter "label=com.docker.compose.oneoff=true"', workflow)
        self.assertIn('docker rm -f "$MIGRATION_CONTAINER"', workflow)
        self.assertIn('MIGRATION_CONTAINER="vstore-migration-', workflow)
        self.assertIn('release_compose run --name "$MIGRATION_CONTAINER"', workflow)

    def test_migration_cleanup_preserves_other_project_containers(self):
        docker = shutil.which('docker')
        if not docker:
            self.skipTest('docker binary not available')
        info = subprocess.run([docker, 'info'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if info.returncode != 0:
            self.skipTest('docker daemon not available')
        images = subprocess.check_output([docker, 'images', '-q'], text=True).splitlines()
        if not images:
            self.skipTest('no docker images available')
        test_image = images[0]

        workflow = (ROOT / '.github/workflows/deploy-service.yml').read_text()
        start = workflow.find('restore_backend_after_migration_failure() {')
        self.assertNotEqual(start, -1)
        end = workflow.find('echo "==> Applying database migrations..."', start)
        self.assertNotEqual(end, -1)
        func_code = workflow[start:end].rstrip()
        func_code = func_code[:func_code.rfind('}') + 1]

        project_target = f'test-vstore-{uuid.uuid4().hex[:8]}'
        project_other = f'test-other-{uuid.uuid4().hex[:8]}'
        container_target = f'vstore-migration-{uuid.uuid4().hex[:8]}'
        container_other = f'{project_other}-backend-run-1'

        try:
            subprocess.run([docker, 'create', '--name', container_target,
                            '--label', f'com.docker.compose.project={project_target}',
                            '--label', 'com.docker.compose.service=backend',
                            '--label', 'com.docker.compose.oneoff=true', test_image],
                           check=True, capture_output=True)
            subprocess.run([docker, 'create', '--name', container_other,
                            '--label', f'com.docker.compose.project={project_other}',
                            '--label', 'com.docker.compose.service=backend',
                            '--label', 'com.docker.compose.oneoff=true', test_image],
                           check=True, capture_output=True)

            bash_script = f"""
            {func_code}
            export MIGRATION_CONTAINER="{container_target}"
            export COMPOSE_PROJECT_NAME="{project_target}"
            export PREV_IMAGE=""
            restore_backend_after_migration_failure "test-failure"
            """
            res = subprocess.run(['bash', '-c', bash_script], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)

            target_ps = subprocess.check_output([docker, 'ps', '-qa', '--filter', f'name=^{container_target}$'], text=True).strip()
            other_ps = subprocess.check_output([docker, 'ps', '-qa', '--filter', f'name=^{container_other}$'], text=True).strip()

            self.assertEqual(target_ps, '')
            self.assertTrue(bool(other_ps))
        finally:
            subprocess.run([docker, 'rm', '-f', container_target, container_other],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

