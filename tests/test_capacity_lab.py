"""Regression tests for production isolation and simultaneous memory accounting."""
import importlib.util
import json
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch
from subprocess import CompletedProcess

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('capacity_lab', ROOT/'scripts/capacity-lab.py')
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class IsolationTests(unittest.TestCase):
    def base(self):
        services = {}
        for name in lab.SERVICES:
            services[name] = {'image': 'production:latest', 'ports': [{'published': '443'}],
                'restart': 'unless-stopped', 'environment': {'SECRET_KEY': 'live-secret'},
                'volumes': [{'type': 'bind', 'source': '/production/storage', 'target': '/app/storage'},
                            {'type': 'volume', 'source': 'data', 'target': '/data'}],
                'networks': {'proxy_net': {'ipv4_address': '172.31.240.5'}},
                'deploy': {'resources': {'limits': {'memory': str(1024*lab.MIB)}}},
                'healthcheck': {'test': ['CMD', 'true']}, 'command': ['uvicorn', 'production-proxies']}
        return {'name': 'vstore', 'services': services,
                'volumes': {'data': {'external': True, 'name': 'production-data'}},
                'networks': {'proxy_net': {'external': True, 'name': 'production'}}}

    def config(self):
        images = {name: {'id': 'sha256:'+name, 'architecture': 'arm64'}
                  for name in ('backend', 'frontend', 'admin', 'proxy', 'blob')}
        return lab.isolated_config(self.base(), images, 'capacity-123456789abc',
            Path('/tmp/lab-report'), '172.29.16.0/28', {'worker_concurrency': 1,
            'memory_mib': {'worker': 384}}, 'test-password', 'test-key')

    def test_no_live_ports_state_or_external_network(self):
        config = self.config()
        self.assertTrue(all('ports' not in service for service in config['services'].values()))
        self.assertTrue(all(not volume for volume in config['volumes'].values()))
        self.assertTrue(all(network['internal'] for network in config['networks'].values()))
        text = str(config)
        for value in ('/production/storage', 'production-data', 'live-secret', '172.31.240.'):
            self.assertNotIn(value, text)
        for service in config['services'].values():
            for mount in service.get('volumes', []):
                if mount['type'] == 'bind' and mount['target'] != '/results':
                    self.assertTrue(mount['read_only'])

    def test_scoped_auth_and_storage_only(self):
        config = self.config()
        for name in ('backend', 'worker', 'probe'):
            env = config['services'][name]['environment']
            self.assertIn('@postgres:5432/capacity', env['DATABASE_URL'])
            self.assertEqual('redis://redis:6379/0', env['CELERY_BROKER_URL'])
            self.assertIn('BlobEndpoint=http://blob:10000/capacity;', env['AZURE_STORAGE_CONNECTION_STRING'])
            self.assertNotIn('GOOGLE_CLIENT_SECRET', env)
        self.assertEqual(['sh', '-c', 'sleep 86400'], config['services']['certbot']['entrypoint'])

    def test_worker_is_bounded_and_profile_is_test_only(self):
        config = self.config()
        self.assertIn('--concurrency=1', config['services']['worker']['command'])
        self.assertEqual(config['services']['backend']['image'], config['services']['worker']['image'])
        self.assertEqual(str(384*lab.MIB), config['services']['worker']['deploy']['resources']['limits']['memory'])
        self.assertEqual(str(1024*lab.MIB), config['services']['backend']['deploy']['resources']['limits']['memory'])

    def test_refuse_non_disposable_project(self):
        with self.assertRaises(ValueError):
            lab.isolated_config(self.base(), {}, 'vstore', Path('/tmp'), '172.29.16.0/28',
                                {'worker_concurrency': 1}, 'password', 'key')

    def test_migration_can_overlap_backend_without_stealing_static_ip(self):
        config = self.config()
        self.assertEqual(['app_net'], config['services']['migration']['networks'])
        self.assertEqual(config['services']['backend']['image'], config['services']['migration']['image'])

    def test_unknown_service_does_not_enter_the_lab_implicitly(self):
        base = self.base()
        base['services']['external-upload'] = {'image': 'unknown', 'environment': {'TOKEN': 'live'}}
        with self.assertRaises(ValueError):
            lab.isolated_config(base, {}, 'capacity-123456789abc', Path('/tmp'), '172.29.16.0/28',
                                {'worker_concurrency': 1}, 'password', 'key')


class AccountingTests(unittest.TestCase):
    def sample(self, backend, worker, phase='mixed'):
        rows = {}
        for name, memory in [('backend', backend), ('worker', worker), ('probe', 999)]:
            rows[name] = {'service': name, 'current': memory, 'working_set': memory,
                'anon': memory, 'rss_sum': memory*3, 'swap': 0,
                'kernel_peak': memory,
                'events': {'oom': 0, 'oom_kill': 0, 'max': 0}}
        return {'phase': phase, 'containers': rows, 'host': {'available': 1000,
                'swap_used': 0, 'psi': {'some': {'total': 10}, 'full': {'total': 5}}}}

    def test_peaks_are_simultaneous_and_exclude_load_generator(self):
        summary = lab.summarize([self.sample(100, 20), self.sample(10, 200)], {})
        # Sum of individual peaks is 300, actual simultaneous peak is 210.
        self.assertEqual(210, summary['phases']['mixed']['stack_working_set_peak'])
        self.assertEqual(100, summary['service_peaks']['backend']['working_set'])
        self.assertEqual(200, summary['service_peaks']['worker']['working_set'])

    def test_deploy_includes_transient_migration(self):
        sample = self.sample(100, 50, 'deploy')
        sample['containers']['migration'] = dict(sample['containers']['backend'], service='migration')
        summary = lab.summarize([sample], {})
        self.assertEqual(250, summary['phases']['deploy']['stack_working_set_peak'])

    def test_refuse_missing_phases_and_fake_success_from_empty_workload(self):
        with self.assertRaises(RuntimeError):
            lab.validate_measurements([], {})
        with self.assertRaises(RuntimeError):
            lab.validate_measurements([self.sample(100, 20)], {})

    def test_reject_limit_pressure_even_without_oom(self):
        row = {'kernel_peak': 80, 'limit': 100, 'max': 0}
        lab.validate_headroom({'service_peaks': {'backend': row}})
        with self.assertRaises(RuntimeError):
            lab.validate_headroom({'service_peaks': {'backend': dict(row, max=1)}})
        with self.assertRaises(RuntimeError):
            lab.validate_headroom({'service_peaks': {'backend': dict(row, kernel_peak=90)}})

    def test_recreation_keeps_surviving_inspect_rows(self):
        for message in ('Error: No such object: removed', 'error: no such object: removed'):
            result = CompletedProcess([], 1, '[{"Id":"surviving"}]', message)
            with self.subTest(message=message), patch.object(lab.subprocess, 'run', return_value=result):
                self.assertEqual([{'Id': 'surviving'}], lab.inspect_containers(['surviving','removed'], True))
                with self.assertRaises(RuntimeError):
                    lab.inspect_containers(['surviving','removed'])

    def test_daemon_failure_is_not_mistaken_for_replacement(self):
        result = CompletedProcess([], 1, '[]', 'Cannot connect to the Docker daemon')
        with patch.object(lab.subprocess, 'run', return_value=result):
            with self.assertRaises(RuntimeError):
                lab.inspect_containers(['backend'], True)


class ProbeTests(unittest.TestCase):
    def test_catalog_and_upload_use_created_uuid_fixtures(self):
        spec = importlib.util.spec_from_file_location('probe', ROOT/'tests/capacity/probe.py')
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        game_ids = ['00000000-0000-0000-0000-000000000001',
                    '00000000-0000-0000-0000-000000000002']
        requests = []

        class Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, url):
                requests.append(('GET', url))
                return SimpleNamespace(status_code=200, json=lambda: [{'id': game_ids[0]}])

            def post(self, url, **kwargs):
                requests.append(('POST', url))
                return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                                       json=lambda: {'access_token': 'test-token'})

        modules = {'httpx': SimpleNamespace(Client=Client),
            'redis': SimpleNamespace(Redis=lambda **kw: SimpleNamespace(set=lambda *a: None)),
            'app.core.db': SimpleNamespace(SessionLocal=None),
            'app.models': SimpleNamespace(GameMedia=None)}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output/'fixtures.json').write_text(json.dumps({'game_ids': game_ids}))
            (output/'image.png').write_bytes(b'synthetic-image')
            (output/'video.mp4').write_bytes(b'synthetic-video')
            with patch.dict('sys.modules', modules), \
                    patch.dict(probe.os.environ, {'ADMIN_PASSWORD': 'test-password'}), \
                    patch.object(probe, 'Path', side_effect=lambda name: output/Path(name).name), \
                    patch('builtins.print'):
                probe.load(SimpleNamespace(seconds=.1, clients=2, uploaders=1,
                                           interval=.001, name='uuid-check'))
            self.assertTrue((output/'uuid-check-load.json').exists())
        for game_id in game_ids:
            self.assertIn(('GET', 'https://proxy/api/v1/games/'+game_id), requests)
        self.assertIn(('POST', 'http://admin:8080/api/v1/admin/games/'+game_ids[0]+'/media'),
                      requests)


if __name__ == '__main__':
    unittest.main()
