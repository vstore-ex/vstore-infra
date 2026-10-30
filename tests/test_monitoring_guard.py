import json
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]

class MonitoringGuardTests(unittest.TestCase):
    def test_every_monitor_is_confined_and_stays_stopped_after_oom(self):
        command = ['docker', 'compose', '--env-file', str(ROOT / '.env.example'),
                   '-f', str(ROOT / 'monitoring/compose.yml'),
                   '-f', str(ROOT / 'monitoring/production.yml'), 'config', '--format', 'json']
        if not (ROOT / 'monitoring/production.yml').exists():
            del command[6:8]
        result = subprocess.run(command, capture_output=True, text=True,
                                env={**os.environ, 'MONITORING_SECRETS_DIR': '/tmp/test-secrets'})
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)['services']
        self.assertEqual(len(services), 9)
        for name, service in services.items():
            self.assertEqual(service['cgroup_parent'], 'vstore-monitoring.slice', name)
            self.assertEqual(service['oom_score_adj'], 500, name)
            self.assertEqual(service['restart'], 'no', name)

if __name__ == '__main__':
    unittest.main()
