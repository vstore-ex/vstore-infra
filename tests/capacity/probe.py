"""Runs only inside the disposable capacity project; no production endpoints."""
import argparse
import concurrent.futures
import json
import os
import random
import subprocess
import threading
import time
from pathlib import Path


def setup():
    from azure.storage.blob import BlobServiceClient
    from app.core.db import SessionLocal
    from app.core.security import get_password_hash
    from app.models import Game, Role, User
    from PIL import Image
    import redis

    BlobServiceClient.from_connection_string(
        os.environ['AZURE_STORAGE_CONNECTION_STRING']).create_container('media')
    with SessionLocal() as db:
        # Current backend seeds its administrator during startup; reuse that fixture.
        role = db.query(Role).filter_by(name='admin').first()
        if role is None:
            role = Role(name='admin')
            db.add(role)
            db.flush()
        user = db.query(User).filter_by(email=os.environ['ADMIN_EMAIL']).first()
        if user is None:
            user = User(email=os.environ['ADMIN_EMAIL'])
            db.add(user)
        user.username = 'capacity'
        user.password = get_password_hash(os.environ['ADMIN_PASSWORD'])
        user.role_id = role.id
        games = [Game(title=f'Capacity game {i}', description_md='Synthetic catalog ' * 256)
                 for i in range(100)]
        db.add_all(games)
        db.flush()
        game_ids = [str(game.id) for game in games]
        db.commit()
    # Modest bounded persistence dataset, including writes during BGSAVE.
    client = redis.Redis(host='redis')
    with client.pipeline(transaction=False) as pipeline:
        for i in range(256):
            pipeline.set(f'capacity:payload:{i}', b'x' * 65536)
        pipeline.execute()
    Image.frombytes('L', (3840, 2160), random.Random(42).randbytes(3840*2160)).convert(
        'RGB').save('/results/image.png')
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', 'testsrc2=size=1920x1080:rate=24', '-t', '5',
                    '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast',
                    '-pix_fmt', 'yuv420p', '-y', '/results/video.mp4'], check=True)
    import hashlib
    import importlib.metadata
    fixture = {'games': 100, 'game_ids': game_ids,
        'redis_payload_mib': 16, 'image_pixels': [3840, 2160],
        'image_seed': 42, 'video': '1080p/24fps/5s',
        'packages': {p: importlib.metadata.version(p) for p in
                     ('celery', 'fastapi', 'Pillow', 'azure-storage-blob', 'SQLAlchemy')},
        'fixtures': {name: {'bytes': Path('/results/'+name).stat().st_size,
                    'sha256': hashlib.sha256(Path('/results/'+name).read_bytes()).hexdigest()}
                    for name in ('image.png', 'video.mp4')}}
    Path('/results/fixtures.json').write_text(json.dumps(fixture, indent=2)+'\n')
    print(json.dumps(fixture))


def load(args):
    import httpx
    import redis
    # Names are resolved solely on this project's network. The runner supplies no URL.
    catalog = 'https://proxy/api/v1/games/'
    admin = 'http://admin:8080/api/v1'
    game_ids = json.loads(Path('/results/fixtures.json').read_text())['game_ids']
    if not game_ids:
        raise RuntimeError('no fixture game IDs; refusing an invalid workload')
    with httpx.Client(verify=False, timeout=60, trust_env=False) as client:
        response = client.post(admin + '/auth/login', data={
            'username': 'capacity', 'password': os.environ['ADMIN_PASSWORD']})
        response.raise_for_status()
        token = response.json()['access_token']
    headers = {'Authorization': f'Bearer {token}'}
    deadline = time.monotonic() + args.seconds
    results = {'catalog': [], 'image': [], 'video': []}
    lock = threading.Lock()
    media_ids = []

    def record(kind, began, code):
        with lock:
            results[kind].append([time.monotonic() - began, code])

    def browse(index):
        with httpx.Client(verify=False, timeout=15, trust_env=False) as client:
            iteration = 0
            while time.monotonic() < deadline:
                began = time.monotonic()
                try:
                    url = catalog + (game_ids[index % len(game_ids)]
                                     if iteration % 2 else '?limit=20')
                    response = client.get(url)
                    code = response.status_code
                    if code == 200:
                        body = response.json()
                        if not body:
                            code = 'empty-response'
                except Exception as exc:
                    code = type(exc).__name__
                record('catalog', began, code)
                iteration += 1
                time.sleep(args.interval)

    def upload(index):
        with httpx.Client(timeout=90, trust_env=False) as client:
            iteration = 0
            while time.monotonic() < deadline:
                kind = 'image' if iteration % 2 == 0 else 'video'
                path = Path('/results/image.png' if kind == 'image' else '/results/video.mp4')
                began = time.monotonic()
                try:
                    with path.open('rb') as payload:
                        response = client.post(admin + f'/admin/games/{game_ids[0]}/media', headers=headers,
                                               data={'media_type': kind},
                                               files={'file': (path.name, payload,
                                                       'image/png' if kind == 'image' else 'video/mp4')})
                    code = response.status_code
                    if code == 200 and kind == 'video':
                        with lock:
                            media_ids.append(response.json()['id'])
                except Exception as exc:
                    code = type(exc).__name__
                record(kind, began, code)
                iteration += 1
                time.sleep(1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.clients + args.uploaders + 1) as pool:
        futures = [pool.submit(browse, i) for i in range(args.clients)]
        futures += [pool.submit(upload, i) for i in range(args.uploaders)]
        # Keep RDB fork's write path active in this isolated Redis instance.
        def mutate():
            client = redis.Redis(host='redis')
            i = 0
            while time.monotonic() < deadline:
                client.set(f'capacity:payload:{i % 256}', os.urandom(65536))
                i += 1
                time.sleep(.1)
        futures.append(pool.submit(mutate))
        for future in futures:
            future.result()
    # An accepted HTTP video upload is insufficient: prove Celery completed each one.
    from app.core.db import SessionLocal
    from app.models import GameMedia
    pending = set(media_ids)
    until = time.monotonic() + 90
    while pending and time.monotonic() < until:
        with SessionLocal() as db:
            for media in db.query(GameMedia).filter(GameMedia.id.in_(pending)).all():
                if media.url != 'processing' and media.thumbnail_url:
                    pending.discard(media.id)
        if pending:
            time.sleep(1)
    summary = {}
    for kind, values in results.items():
        latencies = sorted(x[0] for x in values)
        codes = {}
        for _, code in values:
            codes[str(code)] = codes.get(str(code), 0) + 1
        summary[kind] = {'requests': len(values), 'codes': codes,
                         'p95_seconds': latencies[min(len(latencies)-1, int(len(latencies)*.95))]
                         if latencies else None}
    summary['videos_completed'] = len(media_ids) - len(pending)
    summary['videos_pending'] = len(pending)
    Path(f'/results/{args.name}-load.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary))
    if pending or any(code != '200' for row in summary.values() if isinstance(row, dict)
                      for code in row['codes']):
        raise SystemExit(1)


def key_values(path):
    return {key: int(value) for key, value in (line.split() for line in path.read_text().splitlines())}


def psi(path):
    return {line.split()[0]: {key: float(value) for key, value in
            (pair.split('=') for pair in line.split()[1:])}
            for line in path.read_text().splitlines()}


def collect():
    with Path('/results/samples.jsonl').open('w') as output:
        while not Path('/results/stop').exists():
            state = json.loads(Path('/results/state.json').read_text())
            mem = {line.split(':')[0]: int(line.split()[1])*1024 for line in
                   Path('/proc/meminfo').read_text().splitlines()}
            sample = {'time': time.time(), 'phase': state['phase'], 'host': {
                'total': mem['MemTotal'], 'available': mem['MemAvailable'],
                'swap_used': mem['SwapTotal'] - mem['SwapFree'],
                'psi': psi(Path('/proc/pressure/memory'))}, 'containers': {}}
            for item in state['containers']:
                path = Path('/host-cgroup') / item['cgroup'].lstrip('/')
                try:
                    stat = key_values(path / 'memory.stat')
                    # RSS sum is diagnostic: prefork/shared pages are counted multiple times.
                    rss = 0
                    for pid in (path / 'cgroup.procs').read_text().split():
                        try:
                            status = Path(f'/proc/{pid}/status').read_text().splitlines()
                            rss += sum(int(line.split()[1])*1024 for line in status
                                       if line.startswith('VmRSS:'))
                        except (FileNotFoundError, ProcessLookupError):
                            pass
                    current = int((path / 'memory.current').read_text())
                    sample['containers'][item['name']] = {
                        'service': item['service'], 'current': current,
                        'working_set': max(0, current - stat.get('inactive_file', 0)),
                        'anon': stat['anon'], 'rss_sum': rss,
                        'kernel_peak': int((path / 'memory.peak').read_text()),
                        'swap': int((path / 'memory.swap.current').read_text()),
                        'events': key_values(path / 'memory.events'),
                        'psi': psi(path / 'memory.pressure')}
                except FileNotFoundError:
                    continue  # A completed migration container may already have exited.
            if sample['phase'] != 'recreate' and not any(
                    row['service'] == 'backend' for row in sample['containers'].values()):
                raise RuntimeError('backend cgroup unavailable; refusing empty measurements')
            output.write(json.dumps(sample)+'\n')
            output.flush()
            time.sleep(.5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['setup', 'load', 'collect'])
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--clients', type=int, default=4)
    parser.add_argument('--uploaders', type=int, default=2)
    parser.add_argument('--interval', type=float, default=.2)
    parser.add_argument('--name', default='mixed')
    args = parser.parse_args()
    {'setup': setup, 'load': lambda: load(args), 'collect': collect}[args.action]()
