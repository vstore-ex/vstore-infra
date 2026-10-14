#!/usr/bin/env python3
"""Disposable Docker capacity experiment. Never accepts a remote endpoint or .env."""
import argparse
import base64
import collections
import copy
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ('postgres', 'redis', 'backend', 'worker', 'frontend', 'admin', 'proxy', 'certbot')
HARDENING = {'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true']}
MIB = 1024 * 1024


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def capture(command, **kwargs):
    return run(command, stdout=subprocess.PIPE, **kwargs).stdout.strip()


def write_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2)+'\n')
    temporary.replace(path)


def inspect_containers(ids, allow_missing=False):
    if not ids:
        return []
    inspection = subprocess.run(['docker', 'inspect', *ids], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if inspection.returncode and not (allow_missing and 'no such object:' in inspection.stderr.lower()):
        raise RuntimeError('cannot inspect test containers: '+inspection.stderr)
    return json.loads(inspection.stdout)


def isolated_config(base, images, project, output, subnet, profile, password, key):
    """Start from production topology; erase live state, bindings and destinations."""
    if not project.startswith('capacity-') or len(project) < 15:
        raise ValueError('disposable project prefix required')
    if set(base['services']) != set(SERVICES):
        raise ValueError('review changed production topology before running the laboratory')
    config = copy.deepcopy(base)
    for key_name in tuple(config):
        if key_name.startswith('x-'):
            del config[key_name]
    config['name'] = project
    # Only project-owned, newly created volumes and networks are permitted.
    config['volumes'] = {name: {} for name in base['volumes']}
    config['volumes']['storage'] = {}
    config['volumes']['blob'] = {}
    config['networks'] = {'app_net': {'internal': True},
        'proxy_net': {'internal': True, 'ipam': {'config': [{'subnet': subnet}]}}}
    network_prefix = subnet.rsplit('.', 1)[0]
    for name, service in config['services'].items():
        service.pop('ports', None)
        service.pop('container_name', None)
        service['restart'] = 'no'
        service['pull_policy'] = 'never'
        if profile.get('cpuset_cpus'):
            service['cpuset'] = profile['cpuset_cpus']
        service['environment'] = {}
        if name in images:
            service['image'] = images[name]['id']
            service['platform'] = 'linux/' + images[name]['architecture']
        elif name == 'worker':
            service['image'] = images['backend']['id']
            service['platform'] = 'linux/' + images['backend']['architecture']
        service['volumes'] = [mount for mount in service.get('volumes', [])
                              if mount['type'] != 'bind']
        if name in ('backend', 'worker'):
            service['volumes'].append({'type': 'volume', 'source': 'storage', 'target': '/app/storage'})
            service['environment'] = {
                'DATABASE_URL': 'postgresql://capacity:'+password+'@postgres:5432/capacity',
                'SECRET_KEY': key, 'ADMIN_EMAIL': 'capacity@example.invalid',
                'ADMIN_PASSWORD': password, 'AUTO_MIGRATE': 'false',
                'TRUST_PROXY_HEADERS': 'false', 'CELERY_BROKER_URL': 'redis://redis:6379/0',
                'CELERY_RESULT_BACKEND': 'redis://redis:6379/0',
                'AZURE_STORAGE_CONNECTION_STRING': 'DefaultEndpointsProtocol=http;AccountName=capacity;'
                    'AccountKey='+key+';BlobEndpoint=http://blob:10000/capacity;'}
        if name == 'postgres':
            service['environment'] = {'POSTGRES_DB': 'capacity', 'POSTGRES_USER': 'capacity',
                                      'POSTGRES_PASSWORD': password}
            service['healthcheck']['test'] = ['CMD-SHELL', 'pg_isready -U capacity -d capacity']
        if name == 'proxy':
            # Test mounts include only tracked, read-only proxy configuration.
            service['volumes'] += [
                {'type': 'bind', 'source': str(ROOT/'config/nginx/templates'),
                 'target': '/etc/nginx/templates', 'read_only': True},
                {'type': 'bind', 'source': str(ROOT/'config/nginx/entrypoint/10-init-dummy-certs.sh'),
                 'target': '/docker-entrypoint.d/10-init-dummy-certs.sh', 'read_only': True},
                {'type': 'bind', 'source': str(ROOT/'config/nginx/entrypoint/20-periodic-reload.sh'),
                 'target': '/docker-entrypoint.d/30-periodic-reload.sh', 'read_only': True}]
            service['environment'] = {'FRONTEND_DOMAIN': 'proxy', 'ADMIN_DOMAIN': 'capacity-admin.invalid',
                                      'ADMIN_CERT_NAME': 'proxy',
                                      'NGINX_ENVSUBST_FILTER': 'FRONTEND_DOMAIN|ADMIN_DOMAIN|ADMIN_CERT_NAME'}
        for network in service.get('networks', {}).values():
            if isinstance(network, dict) and 'ipv4_address' in network:
                network['ipv4_address'] = network_prefix + '.' + network['ipv4_address'].split('.')[-1]
        if name == 'backend':
            service['command'][-1] = ','.join(network_prefix + f'.{i}' for i in (2, 3, 4))
        if name in profile.get('memory_mib', {}):
            service['deploy']['resources']['limits']['memory'] = str(profile['memory_mib'][name] * MIB)
    # Production currently has two CPUs. Do not use the Docker Desktop eight-CPU auto default.
    config['services']['worker']['command'] = ['celery', '-A', 'app.worker.celery_app', 'worker',
        '-Q', 'default,media', '--loglevel=info', '-E', '-B',
        '--concurrency='+str(profile['worker_concurrency'])]
    backend = config['services']['backend']
    config['services']['migration'] = copy.deepcopy(backend)
    config['services']['migration']['networks'] = ['app_net']
    config['services']['migration']['healthcheck'] = {'disable': True}
    config['services']['migration']['command'] = ['alembic', 'upgrade', 'head']
    config['services']['probe'] = {
        **HARDENING, 'image': images['backend']['id'], 'platform': backend['platform'],
        'entrypoint': ['python'], 'command': ['/lab/probe.py', 'setup'], 'user': '0:0',
        'environment': {**backend['environment'], 'PYTHONPATH': '/app'},
        'networks': ['app_net', 'proxy_net'],
        'volumes': [{'type': 'bind', 'source': str(ROOT/'tests/capacity/probe.py'),
                     'target': '/lab/probe.py', 'read_only': True},
                    {'type': 'bind', 'source': str(output), 'target': '/results'}],
        'mem_limit': '256m', 'cpus': 1.0, 'healthcheck': {'disable': True}}
    config['services']['blob'] = {
        **HARDENING, 'image': images['blob']['id'], 'platform': 'linux/'+images['blob']['architecture'],
        'environment': {'AZURITE_ACCOUNTS': 'capacity:'+key}, 'networks': ['app_net'],
        'command': ['azurite-blob', '--blobHost', '0.0.0.0', '--skipApiVersionCheck',
                    '--silent', '--location', '/data'],
        'volumes': [{'type': 'volume', 'source': 'blob', 'target': '/data'}],
        # Keep the local storage emulator out of the application's memory bottleneck.
        'mem_limit': '1024m', 'cpus': .5}
    config['services']['init-storage'] = {
        **HARDENING, 'cap_add': ['CHOWN'], 'image': images['backend']['id'],
        'platform': backend['platform'], 'user': '0:0', 'network_mode': 'none',
        'entrypoint': ['chown'], 'command': ['10001:10001', '/storage', '/uploads'],
        'volumes': [{'type': 'volume', 'source': 'storage', 'target': '/storage'},
                    {'type': 'volume', 'source': 'uploads_temp', 'target': '/uploads'}],
        'mem_limit': '64m', 'healthcheck': {'disable': True}}
    # Disable ACME/network activity, retaining the idle production timer's memory shape.
    config['services']['certbot']['entrypoint'] = ['sh', '-c', 'sleep 86400']
    return config


def summarize(samples, limits):
    phases = {}
    service_peaks = collections.defaultdict(lambda: collections.defaultdict(int))
    for sample in samples:
        phase = phases.setdefault(sample['phase'], {'samples': 0, 'stack_working_set_peak': 0,
            'stack_anon_peak': 0, 'stack_current_peak': 0, 'host_available_min': sample['host']['available'],
            'host_swap_peak': 0, 'host_psi_some_total_start': sample['host']['psi']['some']['total'],
            'host_psi_full_total_start': sample['host']['psi']['full']['total']})
        phase['samples'] += 1
        stack = [row for row in sample['containers'].values() if row['service'] in SERVICES
                 or row['service'] == 'migration']
        for metric in ('working_set', 'anon', 'current'):
            phase[f'stack_{metric}_peak'] = max(phase[f'stack_{metric}_peak'],
                                              sum(row[metric] for row in stack))
        phase['host_available_min'] = min(phase['host_available_min'], sample['host']['available'])
        phase['host_swap_peak'] = max(phase['host_swap_peak'], sample['host']['swap_used'])
        for kind in ('some', 'full'):
            phase[f'host_psi_{kind}_delta_us'] = (sample['host']['psi'][kind]['total']
                - phase[f'host_psi_{kind}_total_start'])
        for row in sample['containers'].values():
            service = service_peaks[row['service']]
            for metric in ('current', 'working_set', 'anon', 'rss_sum', 'swap', 'kernel_peak'):
                service[metric] = max(service[metric], row[metric])
            for metric in ('oom', 'oom_kill', 'max'):
                service[metric] = max(service[metric], row['events'].get(metric, 0))
            if row['service'] in limits:
                service['limit'] = limits[row['service']]
    return {'phases': phases, 'service_peaks': dict(service_peaks)}


def validate_measurements(samples, loads):
    if not samples:
        raise RuntimeError('no capacity samples captured')
    for phase in ('idle', 'mixed', 'deploy', 'cooldown', 'recreate'):
        phase_samples = [row for row in samples if row['phase'] == phase]
        if len(phase_samples) < 3:
            raise RuntimeError('insufficient samples for '+phase)
        complete = 0
        for sample in phase_samples:
            observed = {row['service'] for row in sample['containers'].values()}
            missing = set(SERVICES) - observed
            # Only backend/worker may disappear during their deliberate replacement.
            allowed_missing = {'backend', 'worker'} if phase == 'recreate' else set()
            if missing - allowed_missing:
                raise RuntimeError('missing '+phase+' measurements: '+', '.join(sorted(missing)))
            complete += not missing
        if complete < 3:
            raise RuntimeError('insufficient complete stack samples for '+phase+
                               ' (including backend and worker)')
    if not any(row['service'] == 'migration' for sample in samples if sample['phase'] == 'deploy'
               for row in sample['containers'].values()):
        raise RuntimeError('missing stack or deployment overlap measurements')
    for name, load in loads.items():
        for kind in ('catalog', 'image', 'video'):
            if not load[kind]['requests'] or set(load[kind]['codes']) != {'200'}:
                raise RuntimeError('failed '+name+' '+kind+' workload')
        if not load['videos_completed'] or load['videos_pending']:
            raise RuntimeError('incomplete '+name+' video tasks')


def validate_headroom(summary):
    for name, row in summary['service_peaks'].items():
        if name not in SERVICES and name != 'migration':
            continue
        if row['max'] or row['kernel_peak'] > int(row['limit']) * .85:
            raise RuntimeError('less than 15% container headroom or limit pressure: '+name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for service in ('backend', 'frontend', 'admin', 'proxy'):
        parser.add_argument('--'+service+'-image', required=True, help='local test image reference')
    parser.add_argument('--blob-image', default='mcr.microsoft.com/azure-storage/azurite:3.35.0')
    parser.add_argument('--profile', type=Path, help='JSON limits/concurrency; otherwise baseline concurrency=2')
    parser.add_argument('--cpuset-cpus', help='restrict application/migration containers to these host CPUs')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--clients', type=int, default=4)
    parser.add_argument('--uploaders', type=int, default=2)
    args = parser.parse_args()
    if not 30 <= args.seconds <= 600 or not 1 <= args.clients <= 16 or not 1 <= args.uploaders <= 4:
        parser.error('bounded experiment: 30–600 seconds, 1–16 readers, 1–4 uploaders')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error('output directory must be empty')
    profile = json.loads(args.profile.read_text()) if args.profile else {'worker_concurrency': 2}
    if args.cpuset_cpus:
        profile['cpuset_cpus'] = args.cpuset_cpus
    if not 1 <= profile['worker_concurrency'] <= 4:
        parser.error('worker concurrency must be 1–4')
    if any(not 32 <= size <= 2048 for size in profile.get('memory_mib', {}).values()):
        parser.error('test limits must be 32–2048 MiB')
    images = {}
    for service in ('backend', 'frontend', 'admin', 'proxy', 'blob'):
        row = json.loads(capture(['docker', 'image', 'inspect', getattr(args, service+'_image')]))[0]
        images[service] = {'id': row['Id'], 'architecture': row['Architecture'],
                          'revision': (row['Config'].get('Labels') or {}).get('org.opencontainers.image.revision')}
    project = 'capacity-'+secrets.token_hex(6)
    docker_info = json.loads(capture(['docker', 'info', '--format', '{{json .}}']))
    if docker_info['CgroupVersion'] != '2':
        parser.error('Linux cgroup v2 is required; unavailable metrics must not be silently reported as zero')
    # Choose an unused private subnet, never re-use production's static proxy network.
    ids = capture(['docker', 'network', 'ls', '-q']).split()
    used = capture(['docker', 'network', 'inspect', *ids]) if ids else ''
    subnet = next(f'172.29.{i}.0/28' for i in range(16, 240) if f'172.29.{i}.' not in used)
    password = secrets.token_hex(32)
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    write_json(output/'manifest.json', {'project': project, 'images': images, 'profile': profile,
        'infra_revision': capture(['git', 'rev-parse', 'HEAD'], cwd=ROOT),
        'harness_sha256': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in (ROOT/'scripts/capacity-lab.py', ROOT/'tests/capacity/probe.py')},
        'host': {k: docker_info[k] for k in ('OperatingSystem', 'Architecture', 'NCPU', 'MemTotal')},
        'workload': {'seconds_per_phase': args.seconds, 'readers': args.clients,
                     'uploaders': args.uploaders, 'reader_interval_seconds': .2}})
    collector = None
    collector_exit = None
    compose = None
    records = {}
    restart = None
    # A private env/config lives outside the report and is removed even on failure.
    with tempfile.TemporaryDirectory(prefix='vstore-capacity-') as private:
        private = Path(private)
        env_file = private/'lab.env'
        env_file.write_text('\n'.join(f'{k}={v}' for k,v in {
            'POSTGRES_PASSWORD': password, 'DATABASE_URL': 'postgresql://capacity:'+password+'@postgres:5432/capacity',
            'SECRET_KEY': key, 'ADMIN_EMAIL': 'capacity@example.invalid', 'ADMIN_PASSWORD': password}.items()))
        env_file.chmod(0o600)
        env = {k:v for k,v in os.environ.items() if k not in ('COMPOSE_FILE', 'COMPOSE_PROJECT_NAME')}
        # Empty interpolation environment prevents inheritance of live image/DB destinations.
        for k in ('DATABASE_URL', 'POSTGRES_PASSWORD', 'SECRET_KEY', 'ADMIN_EMAIL', 'ADMIN_PASSWORD'):
            env.pop(k, None)
        base = json.loads(capture(['docker', 'compose', '--env-file', str(env_file), '-f',
                                  str(ROOT/'docker-compose.yml'), 'config', '--format', 'json'], env=env))
        config = isolated_config(base, images, project, output, subnet, profile, password, key)
        config_file = private/'compose.json'
        write_json(config_file, config)
        config_file.chmod(0o600)
        compose = ['docker', 'compose', '--env-file', str(env_file), '-p', project, '-f', str(config_file)]
        limits = {name: int(service['deploy']['resources']['limits']['memory'])
                  for name, service in config['services'].items() if 'deploy' in service}

        def state(phase):
            ids = capture(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project='+project]).split()
            # A replacement may delete an old ID between ps and inspect.
            rows = inspect_containers(ids, allow_missing=phase == 'recreate')
            containers = []
            for row in rows:
                service = row['Config']['Labels']['com.docker.compose.service']
                replacing = phase == 'recreate' and service in ('backend', 'worker')
                if service in SERVICES and row['State']['Status'] != 'running' and not replacing:
                    raise RuntimeError('unexpected container exit: '+service)
                pid = row['State']['Pid']
                if not pid:
                    continue
                name = row['Name'].lstrip('/')
                if service == 'backend' and row['Config']['Cmd'][:1] == ['alembic']:
                    service = 'migration'
                records[name] = {'service': service, 'restarts': row['RestartCount'],
                                 'oom': row['State']['OOMKilled'],
                                 'image_id': row['Image'],
                                 'memory_limit': row['HostConfig']['Memory']}
                # Docker's cgroupfs driver and systemd driver use different paths.
                cgroup = '/docker/'+row['Id'] if docker_info['CgroupDriver']=='cgroupfs' else '/system.slice/docker-'+row['Id']+'.scope'
                containers.append({'name': name, 'service': service, 'cgroup': cgroup})
            write_json(output/'state.json', {'phase': phase, 'containers': containers})

        def probe(action, *extra, detach=False):
            return capture(compose+['run', '--no-deps', '--user', '0:0']+(['-d'] if detach else ['--rm'])+
                ['probe', '/lab/probe.py', action, *extra], env=env)

        def await_container(container):
            code = int(capture(['docker', 'wait', container]))
            print(capture(['docker', 'logs', container]), flush=True)
            if code:
                raise RuntimeError('workload or deployment process failed: '+container)

        run(compose+['config', '--quiet'], env=env)
        try:
            run(compose+['run', '--rm', '--no-deps', 'init-storage'], env=env)
            run(compose+['up', '-d', '--wait', '--wait-timeout', '120', 'postgres', 'redis', 'blob'], env=env)
            run(compose+['run', '--rm', '--no-deps', 'migration'], env=env)
            run(compose+['up', '-d', '--wait', '--wait-timeout', '120', *SERVICES], env=env)
            print(probe('setup'), flush=True)
            state('idle')
            collector = capture(['docker', 'run', '-d', '--name', project+'-collector', '--user', '0:0',
                '--pid=host', '--cgroupns=host', '--cap-drop=ALL', '--security-opt', 'no-new-privileges',
                '--memory', '128m', '--cpus', '.5', '--network', 'none',
                '--mount', 'type=bind,src=/sys/fs/cgroup,dst=/host-cgroup,readonly',
                '--mount', f'type=bind,src={output},dst=/results',
                '--mount', f'type=bind,src={ROOT}/tests/capacity/probe.py,dst=/lab/probe.py,readonly',
                '--entrypoint', 'python', images['backend']['id'], '/lab/probe.py', 'collect'])
            time.sleep(10)
            for phase in ('mixed', 'deploy'):
                state(phase)
                container = probe('load', '--name', phase, '--seconds', str(args.seconds),
                                  '--clients', str(args.clients), '--uploaders', str(args.uploaders), detach=True)
                if phase == 'deploy':
                    time.sleep(10)  # Let uploads begin before taking the deployment snapshot.
                    # Same pre-deploy snapshot + Alembic process while existing API/worker are live.
                    run(compose+['exec', '-T', 'postgres', 'bash', '-o', 'pipefail', '-c',
                        'pg_dump -U capacity -d capacity | gzip > /tmp/capacity-snapshot.sql.gz'], env=env)
                    migration = capture(compose+['run', '-d', '--no-deps', 'migration',
                        'sh', '-c', 'alembic upgrade head && sleep 5'], env=env)
                    # Mark this transient explicitly: sh is used to keep the completed migration observable.
                    state('deploy')
                    state_data = json.loads((output/'state.json').read_text())
                    # Compose run names are not derived from the image; inspect the exact migration ID.
                    migration_name = json.loads(capture(['docker','inspect',migration]))[0]['Name'].lstrip('/')
                    for item in state_data['containers']:
                        if item['name'] == migration_name:
                            item['service'] = 'migration'
                    write_json(output/'state.json', state_data)
                    await_container(migration)
                time.sleep(3)
                persistence = capture(compose+['exec', '-T', 'redis', 'redis-cli', 'BGSAVE'], env=env)
                if persistence != 'Background saving started':
                    raise RuntimeError('Redis did not start test persistence: '+persistence)
                await_container(container)
                persistence = capture(compose+['exec', '-T', 'redis', 'redis-cli', '--raw',
                                              'INFO', 'persistence'], env=env)
                persistence = dict(line.split(':',1) for line in persistence.splitlines()
                                   if ':' in line and not line.startswith('#'))
                write_json(output/(phase+'-redis.json'), persistence)
                if persistence.get('rdb_bgsave_in_progress') != '0' or persistence.get('rdb_last_bgsave_status') != 'ok':
                    raise RuntimeError('Redis snapshot did not finish successfully')
                state(phase)
            state('cooldown')
            time.sleep(10)
            # Compose replaces these containers serially; this phase records restart memory.
            # Requests have drained first so this does not assert zero-downtime deployment.
            state('recreate')
            restart = subprocess.Popen(compose+['up', '-d', '--no-deps', '--force-recreate', '--wait',
                                                '--wait-timeout', '120', 'backend', 'worker'], env=env)
            while restart.poll() is None:
                state('recreate')
                time.sleep(1)
            if restart.returncode:
                raise RuntimeError('test stack recreation failed')
            state('recreate')
            time.sleep(10)
        finally:
            if restart and restart.poll() is None:
                # Finish the bounded replacement before project cleanup if sampling failed.
                restart.wait(timeout=150)
            (output/'stop').touch()
            if collector:
                collector_exit = int(capture(['docker', 'wait', collector]))
                if collector_exit:
                    print(capture(['docker', 'logs', collector]))
                run(['docker', 'rm', '-f', collector])
            # Preserve terminal OOM/exit/restart state before deleting only our project.
            ids = capture(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project='+project]).split()
            if ids:
                for row in json.loads(capture(['docker', 'inspect', *ids])):
                    records[row['Name'].lstrip('/')] = {'service': row['Config']['Labels']['com.docker.compose.service'],
                        'restarts': row['RestartCount'], 'oom': row['State']['OOMKilled'],
                        'memory_limit': row['HostConfig']['Memory'],
                        'image_id': row['Image'],
                        'exit_code': row['State']['ExitCode'], 'status': row['State']['Status']}
            write_json(output/'containers.json', records)
            run(compose+['down', '-v', '--remove-orphans'], env=env)
            if (output/'samples.jsonl').exists():
                samples = [json.loads(line) for line in (output/'samples.jsonl').read_text().splitlines()]
                if samples:
                    write_json(output/'summary.json', summarize(samples, limits))
    print('Results: '+str(output), flush=True)
    if collector_exit != 0:
        raise RuntimeError('metrics collector failed')
    samples = [json.loads(line) for line in (output/'samples.jsonl').read_text().splitlines()]
    loads = {phase: json.loads((output/(phase+'-load.json')).read_text()) for phase in ('mixed','deploy')}
    validate_measurements(samples, loads)
    summary = json.loads((output/'summary.json').read_text())
    if any(row['oom'] or row['oom_kill'] for row in summary['service_peaks'].values()):
        raise RuntimeError('OOM during capacity experiment')
    if any(row['oom'] or row['restarts'] for row in records.values()):
        raise RuntimeError('unexpected container restart or OOM')
    for row in records.values():
        if row['service'] in SERVICES and row.get('status') != 'running':
            raise RuntimeError('unexpected container exit: '+row['service'])
    # Baseline may expose poor margins; a proposed profile must meet its explicit reserve.
    if args.profile:
        validate_headroom(summary)


if __name__ == '__main__':
    main()
