import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('database_recovery', ROOT / 'scripts/database-recovery.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
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
