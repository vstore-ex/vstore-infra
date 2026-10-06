import importlib.util
import json
from pathlib import Path
import ssl
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('proxy_health', Path(__file__).resolve().parents[1] / 'scripts/proxy-health.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class ProxyHealthTests(unittest.TestCase):
    def test_invalid_catalog_and_static_html_fail(self):
        for status, content_type, payload in ((502, 'text/html', b'bad'), (200, 'text/html', b'<html>SPA</html>'),
                                              (200, 'application/json', b'{}'), (200, 'application/json', b'bad')):
            with patch.object(proxy, 'LocalHTTPS') as connection:
                response = connection.return_value.getresponse.return_value
                response.status = status
                response.getheader.return_value = content_type
                response.read.return_value = payload
                with self.assertRaises(ValueError):
                    proxy.verify_api('example.com', 443, ssl.create_default_context())

    def test_both_routes_must_pass_same_attempt(self):
        endpoints = [('front', 443, None), ('admin', 443, None)]
        with patch.object(proxy, 'targets', return_value=endpoints), patch.object(proxy, 'verify_api', side_effect=[None, ValueError('admin broken')]):
            with self.assertRaises(ValueError):
                proxy.verify(attempts=1)

    def test_public_domains_keep_system_ca_and_hostname_checks(self):
        config = {'services': {'proxy': {'environment': {'FRONTEND_DOMAIN': 'store.example.com',
                                                       'ADMIN_DOMAIN': 'admin.example.com',
                                                       'ADMIN_CERT_NAME': 'store.example.com'}}}}
        with patch.object(proxy, 'compose', side_effect=[json.dumps(config), '0.0.0.0:443']):
            endpoints = list(proxy.targets())
        for domain, port, context in endpoints:
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
