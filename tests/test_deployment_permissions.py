from concurrent.futures import ThreadPoolExecutor
import fcntl
import importlib.util
import os
from pathlib import Path
import stat
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
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

    def test_lock_only_leaves_secrets_and_backups_for_locked_preflight(self):
        lock = self.root / '.deploy.lock'
        lock.write_text('')
        lock.chmod(0o644)
        override = self.root / 'docker-compose.override.yml'
        override.write_text('{}')
        override.chmod(0o644)
        permissions.secure(self.root, lock_only=True)
        permissions.secure(self.root, check=True, lock_only=True)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        for path in (self.root / '.env', override, self.snapshot):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.snapshot.unlink()
        (self.root / 'backups').rmdir()
        permissions.secure(self.root, lock_only=True)
        self.assertFalse((self.root / 'backups').exists())

    def test_lock_only_refuses_hardlinked_lock_without_changing_other_copy(self):
        lock = self.root / '.deploy.lock'
        lock.write_text('')
        lock.chmod(0o644)
        outside = Path(self.temp.name) / 'other-lock-copy'
        os.link(lock, outside)
        with self.assertRaises(ValueError):
            permissions.secure(self.root, lock_only=True)
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)

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

    def test_both_ssh_workflows_wait_for_snapshot_rename_and_retention(self):
        shutil.copytree(ROOT / 'scripts', self.root / 'scripts')
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        # A scheduling barrier reproduces removal after listdir if a workflow
        # still traverses backups before flock. All file changes and locks are real.
        python = bin_dir / 'python3'
        python.write_text('#!' + sys.executable + '\n' + textwrap.dedent('''\
            import os, runpy, sys, time
            from pathlib import Path
            original_listdir = os.listdir
            backup_inode = Path('backups').stat().st_ino
            def listdir(path):
                names = original_listdir(path)
                if isinstance(path, int) and os.fstat(path).st_ino == backup_inode and not Path('.writer_finished').exists():
                    Path('.backups_enumerated').touch()
                    deadline = time.monotonic() + 5
                    while not Path('.writer_finished').exists():
                        if time.monotonic() >= deadline:
                            raise TimeoutError('snapshot writer did not finish')
                        time.sleep(0.01)
                return names
            os.listdir = listdir
            sys.argv = sys.argv[1:]
            runpy.run_path(sys.argv[0], run_name='__main__')
            '''))
        python.chmod(0o755)
        # Use the same blocking flock primitive on Linux and macOS.
        flock = bin_dir / 'flock'
        flock.write_text('#!' + sys.executable + '\n' + textwrap.dedent('''\
            import fcntl, sys
            from pathlib import Path
            Path('.lock_waiting').touch()
            fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX)
            '''))
        flock.chmod(0o755)
        env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'])
        for name in ('ci.yml', 'deploy-service.yml'):
            with self.subTest(workflow=name):
                for marker in ('.writer_finished', '.backups_enumerated', '.lock_waiting'):
                    (self.root / marker).unlink(missing_ok=True)
                temporary = self.root / 'backups/pre_deploy_fixture_TMP'
                temporary.write_bytes(b'new-snapshot')
                temporary.chmod(0o644)
                final = temporary.with_suffix('.sql.gz')
                self.snapshot.write_bytes(b'expired-snapshot')
                workflow = (ROOT / '.github/workflows' / name).read_text()
                lines = [line[12:] for line in workflow.split('          script: |\n', 1)[1].splitlines()]
                lock_line = next(i for i, line in enumerate(lines) if line.startswith('flock -w'))
                post_lock = next(line for line in lines[lock_line + 1:] if line.startswith('python3 scripts/secure-deployment.py'))
                # Skip deployment actions; execute the actual preflight/lock and
                # post-lock permission command from each workflow.
                script = '\n'.join(lines[:lock_line + 1] + [post_lock, 'echo LOCKED_PREFLIGHT_DONE'])
                script = script.replace('${{ secrets.DEPLOY_PATH }}', str(self.root))
                with open(self.root / '.deploy.lock', 'a') as held_lock:
                    fcntl.flock(held_lock, fcntl.LOCK_EX)
                    def finish_snapshot():
                        try:
                            deadline = time.monotonic() + 5
                            while not any((self.root / marker).exists() for marker in ('.lock_waiting', '.backups_enumerated')):
                                if time.monotonic() >= deadline:
                                    raise TimeoutError('waiting deployment did not reach preflight or flock')
                                time.sleep(0.01)
                            temporary.rename(final)
                            self.snapshot.unlink()
                        finally:
                            (self.root / '.writer_finished').touch()
                            fcntl.flock(held_lock, fcntl.LOCK_UN)
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        writer = executor.submit(finish_snapshot)
                        result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, timeout=10)
                        writer.result(timeout=1)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('LOCKED_PREFLIGHT_DONE', result.stdout)
                self.assertTrue((self.root / '.lock_waiting').exists())
                self.assertFalse((self.root / '.backups_enumerated').exists())
                self.assertEqual(final.read_bytes(), b'new-snapshot')
                self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o600)
                self.assertFalse(self.snapshot.exists())

    def test_special_file_is_rejected_without_blocking(self):
        os.mkfifo(self.root / 'backups/fifo')
        with self.assertRaises(ValueError):
            permissions.secure(self.root)


if __name__ == '__main__':
    unittest.main()
