"""Safety and offline/boot scheduling regressions for encrypted recovery points."""
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('offhost', ROOT / 'scripts/offhost-backup.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class OffhostBackupTests(unittest.TestCase):
    def test_week_offline_is_due_on_next_boot(self):
        now = datetime(2026, 10, 6, tzinfo=timezone.utc)
        for hours, due in ((1, False), (167.99, False), (168, True), (240, True)):
            item = {'time': (now - timedelta(hours=hours)).isoformat()}
            self.assertEqual(backup.age_hours([item], now) >= 168, due)
        self.assertIsNone(backup.age_hours([], now))

    def test_latest_completed_recovery_point_controls_age(self):
        now = datetime(2026, 10, 6, tzinfo=timezone.utc)
        self.assertEqual(backup.age_hours([{'time': (now - timedelta(days=10)).isoformat()},
                                          {'time': (now - timedelta(days=2)).isoformat()}], now), 48)

    def test_future_or_naive_timestamp_is_not_protection(self):
        now = datetime(2026, 10, 6, tzinfo=timezone.utc)
        for date in (now + timedelta(hours=1), now.replace(tzinfo=None)):
            with self.assertRaises(ValueError):
                backup.age_hours([{'time': date.isoformat()}], now)

    def test_pending_snapshots_do_not_control_next_boot(self):
        with patch.object(backup, 'restic', return_value=b'[]') as restic:
            self.assertEqual(backup.snapshots({'host': 'vstore-prod'}), [])
        self.assertIn(backup.TAG, restic.call_args.args)
        self.assertNotIn(backup.PENDING_TAG, restic.call_args.args)

    def test_added_writable_state_or_external_broker_cannot_be_silently_omitted(self):
        targets = {'postgres': ['/var/lib/postgresql/data'], 'redis': ['/data'],
                   'backend': ['/app/storage', '/tmp/uploads'], 'worker': ['/app/storage', '/tmp/uploads'],
                   'proxy': ['/etc/letsencrypt', '/var/www/certbot']}
        resolved = {'services': {service: {'volumes': [{'target': target} for target in paths]}
                                 for service, paths in targets.items()}}
        resolved['services']['backend']['environment'] = {}
        backup.validate_topology(resolved)
        resolved['services']['worker']['volumes'].append({'target': '/new-persistent-state'})
        with self.assertRaises(ValueError):
            backup.validate_topology(resolved)
        resolved['services']['worker']['volumes'].pop()
        resolved['services']['backend']['environment']['CELERY_BROKER_URL'] = 'redis://outside:6379/0'
        with self.assertRaises(ValueError):
            backup.validate_topology(resolved)

    def test_partial_upload_never_publishes_completed_tag(self):
        with patch.object(backup, 'restic', side_effect=RuntimeError('exit 3')) as restic:
            with self.assertRaises(RuntimeError):
                backup.publish({'host': 'test'}, Path('/tmp/bundle'), datetime.now(timezone.utc).isoformat())
        self.assertEqual(restic.call_count, 1)
        self.assertIn(backup.PENDING_TAG, restic.call_args.args)
        self.assertNotIn(backup.TAG, restic.call_args.args)

    def test_fresh_backup_skips_without_stopping_writers(self):
        with patch.object(backup.permissions, 'secure'), \
                patch.object(backup.release, 'locked', return_value=contextlib.nullcontext()), \
                patch.object(backup, 'resume_pending'), \
                patch.object(backup, 'snapshots', return_value=[]), \
                patch.object(backup, 'age_hours', return_value=10), \
                patch.object(backup, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
            backup.backup({'interval_hours': 168}, if_due=True)
        run.assert_not_called()

    def test_unreachable_repository_does_not_stop_writers(self):
        with patch.object(backup.permissions, 'secure'), \
                patch.object(backup.release, 'locked', return_value=contextlib.nullcontext()), \
                patch.object(backup, 'resume_pending') as resume, \
                patch.object(backup, 'snapshots', side_effect=RuntimeError('offline')), \
                patch.object(backup, 'run') as run:
            with self.assertRaises(RuntimeError):
                backup.backup({'interval_hours': 168}, if_due=True)
        resume.assert_called_once()
        run.assert_not_called()

    def test_private_config_and_production_offhost_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            password = directory / 'password'
            password.write_text('disposable-fixture-only')
            password.chmod(0o600)
            config = directory / 'config.json'
            config.write_text(json.dumps({'host': 'test', 'repository': str(directory / 'repo'),
                                         'password_file': str(password), 'interval_hours': 168, 'keep_weekly': 4}))
            config.chmod(0o600)
            with self.assertRaises(ValueError):
                backup.configuration(config)
            with patch.dict(os.environ, {'RESTIC_PASSWORD': 'must-not-inherit', 'AZURE_ACCOUNT_KEY': 'must-not-inherit'}):
                value = backup.configuration(config, test_local=True)
            self.assertNotIn('RESTIC_PASSWORD', value['environment'])
            self.assertNotIn('AZURE_ACCOUNT_KEY', value['environment'])
            password.chmod(0o644)
            with self.assertRaises(ValueError):
                backup.configuration(config, test_local=True)

    def test_password_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'target').write_text('fixture')
            (path / 'link').symlink_to('target')
            with self.assertRaises(OSError):
                backup.private_file(path / 'link')

    def check_archive(self, members, success):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'archive.tar.gz'
            with tarfile.open(path, 'w:gz') as stream:
                for name, target in members:
                    item = tarfile.TarInfo(name)
                    if target is not None:
                        item.type, item.linkname = tarfile.SYMTYPE, target
                    stream.addfile(item)
            if success:
                backup.validate_tar(path)
            else:
                with self.assertRaises(ValueError):
                    backup.validate_tar(path)

    def test_certificate_links_inside_archive_are_preserved(self):
        self.check_archive([('archive/site/key.pem', None), ('live/site/key.pem', '../../archive/site/key.pem')], True)

    def test_archive_traversal_and_symlink_pivots_are_refused(self):
        for members in ([('../escape', None)], [('/escape', None)],
                        [('link', '../escape')], [('link', '.'), ('link/file', None)],
                        [('link', '.'), ('other', 'link/../escape')]):
            self.check_archive(members, False)

    def test_restore_requires_explicit_snapshot_and_new_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with self.assertRaises(ValueError):
                backup.restore_bundle({}, 'latest', target)
            with patch.object(backup, 'snapshots', return_value=[{'id': 'a' * 64}]), \
                    patch.object(backup, 'restic') as restic:
                with self.assertRaises(FileExistsError):
                    backup.restore_bundle({}, 'a' * 64, target)
            restic.assert_not_called()

    def test_timer_checks_boot_and_retries_with_persistent_history(self):
        timer = (ROOT / 'config/backup/vstore-backup.timer').read_text()
        self.assertIn('OnBootSec=1min', timer)
        self.assertIn('OnUnitInactiveSec=15min', timer)
        self.assertIn('Persistent=true', timer)
        service = (ROOT / 'config/backup/vstore-backup.service').read_text()
        self.assertIn('backup --if-due', service)
        self.assertIn('After=network-online.target docker.service', service)
        self.assertIn('ExecStopPost=', service)

    def test_deploy_gate_is_under_shared_lock(self):
        for filename in ('ci.yml', 'deploy-service.yml'):
            workflow = (ROOT / '.github/workflows' / filename).read_text()
            self.assertLess(workflow.index('flock -w 300 200'), workflow.index('if [ -f backups/.offhost-resume.json ]'))

    def test_resume_journal_write_failure_never_publishes_partial_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resume.json'
            with patch.object(backup, 'RESUME', path), patch.object(backup.os, 'fsync', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    backup.remember_running({'backend': 'a' * 64})
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_disappeared_container_is_refused_before_marker_or_downtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resume.json'
            with patch.object(backup, 'RESUME', path):
                with self.assertRaises(ValueError):
                    backup.remember_running({'backend': 'a' * 64, 'worker': ''})
            self.assertFalse(path.exists())

    def test_forced_or_failed_writer_stop_cannot_be_a_consistent_recovery_point(self):
        for state in ({'Running': False, 'ExitCode': 137}, {'Running': False, 'ExitCode': 1},
                      {'Running': False, 'ExitCode': 0, 'OOMKilled': True}, {'Running': True, 'ExitCode': 0}):
            with patch.object(backup, 'run', return_value=json.dumps(state).encode()):
                with self.assertRaises(ValueError):
                    backup.ensure_quiesced({'worker': 'a' * 64})
        for code in (0, 143):
            with patch.object(backup, 'run', return_value=json.dumps({'Running': False, 'ExitCode': code}).encode()):
                backup.ensure_quiesced({'worker': 'a' * 64})

    def test_manual_release_mutation_refuses_interrupted_backup(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                Path('backups').mkdir()
                Path('backups/.offhost-resume.json').write_text('{}')
                with self.assertRaises(ValueError):
                    with backup.release.locked(None):
                        self.fail('manual mutation must not execute')
                with backup.release.locked(None, allow_backup_resume=True):
                    pass
            finally:
                os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
