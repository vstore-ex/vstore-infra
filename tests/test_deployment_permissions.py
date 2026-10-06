import importlib.util
import os
from pathlib import Path
import stat
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('permissions', ROOT / 'scripts/secure-deployment.py')
permissions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(permissions)


class DeploymentPermissionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'deploy'
        self.root.mkdir(mode=0o777)
        self.root.chmod(0o777)
        (self.root / '.env').write_text('FIXTURE_SECRET=do-not-print-this-value\n')
        (self.root / '.env').chmod(0o644)
        (self.root / 'backups').mkdir()
        self.snapshot = self.root / 'backups/snapshot.sql.gz'
        self.snapshot.write_bytes(b'fixture')
        self.snapshot.chmod(0o644)

    def test_existing_snapshots_and_metadata_become_private_and_storage_unchanged(self):
        (self.root / 'backups/retained').mkdir()
        metadata = self.root / 'backups/retained/snapshot.metadata.json'
        metadata.write_text('{}')
        storage = self.root / 'storage'
        storage.mkdir(mode=0o755)
        before = storage.stat()
        permissions.secure(self.root)
        permissions.secure(self.root, check=True)
        for path, mode in ((self.root, 0o700), (self.root / '.env', 0o600),
                           (self.snapshot, 0o600), (metadata, 0o600), (metadata.parent, 0o700)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
        self.assertEqual((storage.stat().st_mode, storage.stat().st_uid), (before.st_mode, before.st_uid))

    def test_missing_backup_directory_is_created_private(self):
        self.snapshot.unlink()
        (self.root / 'backups').rmdir()
        permissions.secure(self.root)
        self.assertEqual(stat.S_IMODE((self.root / 'backups').stat().st_mode), 0o700)

    def test_check_does_not_repair_or_print_secrets(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/secure-deployment.py'),
                                 '--directory', str(self.root), '--check'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o777)
        self.assertNotIn('do-not-print-this-value', result.stdout + result.stderr)

    def test_secret_and_snapshot_symlinks_never_change_external_targets(self):
        outside = Path(self.temp.name) / 'outside'
        outside.write_text('external')
        outside.chmod(0o644)
        for path in (self.root / '.env', self.snapshot):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.unlink()
                path.symlink_to(outside)
                with self.assertRaises(OSError):
                    permissions.secure(self.root)
                self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)
                path.unlink()
                path.write_bytes(original)

    def test_hardlinked_snapshot_is_refused_without_chmod_of_other_copy(self):
        outside = Path(self.temp.name) / 'other-copy'
        os.link(self.snapshot, outside)
        with self.assertRaises(ValueError):
            permissions.secure(self.root)
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)

    def test_backup_directory_symlink_is_refused(self):
        self.snapshot.unlink()
        (self.root / 'backups').rmdir()
        outside = Path(self.temp.name) / 'external-backups'
        outside.mkdir(mode=0o755)
        (self.root / 'backups').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            permissions.secure(self.root)
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)

    def test_both_ssh_workflows_refuse_symlink_lock_before_shell_truncation(self):
        shutil.copytree(ROOT / 'scripts', self.root / 'scripts')
        victim = Path(self.temp.name) / 'unrelated-file'
        victim.write_bytes(b'must-stay-unchanged')
        (self.root / '.deploy.lock').symlink_to(victim)
        for name in ('ci.yml', 'deploy-service.yml'):
            with self.subTest(workflow=name):
                workflow = (ROOT / '.github/workflows' / name).read_text()
                script = workflow.split('          script: |\n', 1)[1]
                prefix = script.split('            exec 200>', 1)[0]
                prefix = '\n'.join(line[12:] for line in prefix.splitlines())
                prefix = prefix.replace('${{ secrets.DEPLOY_PATH }}', str(self.root))
                result = subprocess.run(['bash', '-c', prefix + '\nexec 200>.deploy.lock\necho LOCK_OPENED'],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('LOCK_OPENED', result.stdout)
                self.assertEqual(victim.read_bytes(), b'must-stay-unchanged')

    def test_special_file_is_rejected_without_blocking(self):
        os.mkfifo(self.root / 'backups/fifo')
        with self.assertRaises(ValueError):
            permissions.secure(self.root)


if __name__ == '__main__':
    unittest.main()
