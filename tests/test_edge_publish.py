"""Execute the CI publisher shell with controlled Docker responses."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCANNED = 'sha256:' + 'a' * 64
DIGEST = 'sha256:' + 'b' * 64
REPOSITORY = 'ghcr.io/vstore-ex/vstore-infra-proxy'


class EdgePublishTests(unittest.TestCase):
    def run_publish(self, changed_tag=False, changed_registry=False, failed_push=False):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        block = workflow.split('      - name: Publish the exact scanned artifact\n', 1)[1].split('\n\n  deploy:', 1)[0]
        script = '\n'.join(line[10:] for line in block.split('        run: |\n', 1)[1].splitlines())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'docker'
            executable.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['EVENTS'],'a') as stream: stream.write(json.dumps(args)+'\\n')
if args[:2] == ['image','inspect']:
 if '.RepoDigests' in args[-1]:
  print(json.dumps(['localhost:5000/unrelated@'+os.environ['DIGEST'], os.environ['REPOSITORY']+'@'+os.environ['DIGEST']]))
 else:
  changed = (args[2]=='local-edge:scan' and os.environ['CHANGED_TAG']=='1') or ('@sha256:' in args[2] and os.environ['CHANGED_REGISTRY']=='1')
  print('sha256:'+'c'*64 if changed else os.environ['SCANNED'])
elif args[0] == 'push' and os.environ['FAILED_PUSH']=='1':
 sys.exit(1)
''')
            executable.chmod(0o755)
            output = root / 'output'
            environment = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                           'EDGE_IMAGE_ID': SCANNED, 'IMAGE_TAG': REPOSITORY + ':sha-test',
                           'IMAGE_REPOSITORY': REPOSITORY, 'GITHUB_OUTPUT': str(output),
                           'EVENTS': str(root / 'events'), 'SCANNED': SCANNED, 'DIGEST': DIGEST,
                           'REPOSITORY': REPOSITORY, 'CHANGED_TAG': str(int(changed_tag)),
                           'CHANGED_REGISTRY': str(int(changed_registry)), 'FAILED_PUSH': str(int(failed_push))}
            result = subprocess.run(['bash', '-c', script], env=environment, capture_output=True, text=True)
            events = [json.loads(line) for line in (root / 'events').read_text().splitlines()]
            return result.returncode, output.read_text() if output.exists() else '', events

    def test_publishes_scanned_id_and_repository_digest(self):
        code, output, events = self.run_publish()
        self.assertEqual(code, 0)
        self.assertEqual(output, 'image=' + REPOSITORY + '@' + DIGEST + '\n')
        self.assertIn(['tag', SCANNED, REPOSITORY + ':sha-test'], events)

    def test_changed_scan_tag_cannot_publish(self):
        code, output, events = self.run_publish(changed_tag=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(output, '')
        self.assertFalse(any(event[0] == 'push' for event in events))

    def test_wrong_published_identity_or_failed_push_has_no_deploy_output(self):
        for options in ({'changed_registry': True}, {'failed_push': True}):
            with self.subTest(options=options):
                code, output, events = self.run_publish(**options)
                self.assertNotEqual(code, 0)
                self.assertEqual(output, '')
