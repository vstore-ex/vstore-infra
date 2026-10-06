#!/usr/bin/env python3
"""Refresh Docker DNS in dependent Nginx workers and verify both TLS API routes."""
import argparse
import http.client
import json
import socket
import ssl
import subprocess
import sys
import time


def compose(*args):
    return subprocess.check_output(['docker', 'compose', '-f', 'docker-compose.yml', *args], text=True).strip()


def reload_proxies():
    # Validate every configuration before signalling any worker. App proxies must
    # resolve backend first; edge then resolves the current frontend/admin IPs.
    for service in ('frontend', 'admin', 'proxy'):
        compose('exec', '-T', service, 'nginx', '-t')
    for service in ('frontend', 'admin', 'proxy'):
        compose('exec', '-T', service, 'nginx', '-s', 'reload')


class LocalHTTPS(http.client.HTTPSConnection):
    def __init__(self, domain, port, context):
        super().__init__(domain, port=port, timeout=5, context=context)

    def connect(self):
        connection = socket.create_connection(('127.0.0.1', self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
        except Exception:
            connection.close()
            raise


def targets():
    config = json.loads(compose('config', '--format', 'json'))
    environment = config['services']['proxy']['environment']
    port = int(compose('port', 'proxy', '443').splitlines()[0].rsplit(':', 1)[1])
    frontend = environment['FRONTEND_DOMAIN']
    for domain, certificate in ((frontend, frontend), (environment['ADMIN_DOMAIN'], environment['ADMIN_CERT_NAME'])):
        context = ssl.create_default_context()
        if domain.endswith(('.local', '.localhost', '.test')):
            # Local admin policy may intentionally reuse the storefront dummy
            # certificate. Authenticate the locally mounted certificate, without
            # requiring a public hostname/SAN. Public domains keep normal PKI.
            pem = compose('exec', '-T', 'proxy', 'cat', f'/etc/letsencrypt/live/{certificate}/fullchain.pem')
            context.load_verify_locations(cadata=pem)
            context.check_hostname = False
        yield domain, port, context


def verify_api(domain, port, context):
    connection = LocalHTTPS(domain, port, context)
    try:
        connection.request('GET', '/api/v1/games/?limit=1', headers={'Accept': 'application/json'})
        response = connection.getresponse()
        payload = response.read(1024 * 1024 + 1)
        if response.status != 200 or response.getheader('Content-Type', '').split(';')[0] != 'application/json':
            raise ValueError(f'{domain}: API status/content type failed ({response.status})')
        if len(payload) > 1024 * 1024 or not isinstance(json.loads(payload), list):
            raise ValueError(f'{domain}: API returned an invalid catalog response')
    finally:
        connection.close()


def verify(attempts=15, delay=3):
    endpoints = list(targets())
    for attempt in range(attempts):
        try:
            for endpoint in endpoints:
                verify_api(*endpoint)
            print('PASS: storefront and admin HTTPS API routes')
            return
        except (OSError, ValueError, http.client.HTTPException) as error:
            if attempt + 1 == attempts:
                raise ValueError(f'HTTPS API gate failed: {error}') from error
            time.sleep(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('recover', 'verify'))
    parser.add_argument('--attempts', type=int, default=15)
    parser.add_argument('--delay', type=float, default=3)
    args = parser.parse_args()
    if args.attempts < 1 or args.delay < 0:
        parser.error('attempts must be positive and delay nonnegative')
    if args.command == 'recover':
        reload_proxies()
    verify(args.attempts, args.delay)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
