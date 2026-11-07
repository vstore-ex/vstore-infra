"""Execute the real SSH revision-sync block against a disposable local remote."""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InfraRevisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.git('init', '--initial-branch=main')
        self.git('config', 'user.name', 'Revision regression')
        self.git('config', 'user.email', 'revision@example.invalid')
        self.first = self.commit('initial')
        self.remote = self.root / 'remote.git'
        self.git('init', '--bare', '--initial-branch=main', str(self.remote))
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', 'origin', 'main')
        self.host = self.root / 'host'
        self.git('clone', str(self.remote), str(self.host))
        self.validated = self.commit('validated')
        self.git('push', 'origin', 'main')
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        block = workflow.split('            PREV_GIT_COMMIT=', 1)[1].split('            # Adopt healthy', 1)[0]
        self.script = 'set -euo pipefail\n' + textwrap.dedent('            PREV_GIT_COMMIT=' + block)

    def git(self, *args, cwd=None):
        return subprocess.check_output(['git', *args], cwd=cwd or self.source, text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self, value):
        (self.source / 'config').write_text(value)
        self.git('add', 'config')
        self.git('commit', '-m', value)
        return self.git('rev-parse', 'HEAD')

    def sync(self, sha):
        return subprocess.run(['bash', '-c', self.script], cwd=self.host,
                              env={**os.environ, 'DEPLOY_SHA': sha}, text=True, capture_output=True)

    def test_current_run_applies_and_records_exact_validated_sha(self):
        result = self.sync(self.validated)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.host), self.validated)
        self.assertIn('Checked-out infrastructure commit: ' + self.validated, result.stdout)

    def test_advancing_main_rejects_obsolete_run_without_changing_host(self):
        self.commit('newer unvalidated config')
        self.git('push', 'origin', 'main')
        result = self.sync(self.validated)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Obsolete infrastructure run', result.stdout)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.host), self.first)
        self.assertEqual((self.host / 'config').read_text(), 'initial')

    def test_missing_malformed_and_unknown_revisions_leave_host_untouched(self):
        for sha in ('', 'main', 'HEAD; touch injected', 'f' * 40):
            with self.subTest(sha=sha):
                self.assertNotEqual(self.sync(sha).returncode, 0)
                self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.host), self.first)
                self.assertFalse((self.host / 'injected').exists())

    def test_workflow_forwards_validated_sha_to_ssh(self):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn('DEPLOY_SHA: ${{ github.sha }}', workflow)
        self.assertIn('envs: EDGE_IMAGE,DEPLOY_SHA', workflow)
        self.assertIn("-p 'test_infra_revision.py' -v", workflow)


if __name__ == '__main__':
    unittest.main()
