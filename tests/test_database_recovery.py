import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

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
