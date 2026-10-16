#!/usr/bin/env python3
"""Offline contract regression checks; no cluster writes or network requests."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('service', Path(__file__).with_name('firecrawl-service.py'))
assert spec is not None and spec.loader is not None
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class ContractTests(unittest.TestCase):
    def config(self, **kw):
        return dict(mode='managed-install', endpoint='https://crawl.example.net',
                    auth='bearer-proxy', namespace='shared-web', postgres_secret='existing-queue',
                    ingress_namespace='authorized-proxy', searxng_endpoint='http://search.search:8080', **kw)

    def test_existing_never_renders_or_probes(self):
        with patch.object(service.subprocess, 'run', side_effect=AssertionError('must not run')):
            result = service.render({'mode': 'existing-service', 'endpoint': 'https://unreachable.example',
                                     'auth': 'network-restricted'})
        self.assertEqual(result['manifests'], [])

    def test_explicit_mode_and_auth(self):
        for key, value in [('mode', 'auto'), ('auth', 'none')]:
            config = self.config()
            config[key] = value
            with self.assertRaises(ValueError):
                service.render(config)

    def test_bad_endpoints(self):
        for endpoint in ['https://user:password@example.net', 'file:///etc/passwd',
                         'https://example.net?token=bad', 'https://example.net#bad']:
            with self.assertRaises(ValueError):
                service.url(endpoint)

    def test_required_managed_inputs(self):
        for key in ['namespace', 'postgres_secret', 'searxng_endpoint', 'ingress_namespace']:
            config = self.config()
            del config[key]
            with self.assertRaises(KeyError):
                service.render(config)

    def test_managed_is_deterministic_and_standalone(self):
        first = service.render(self.config())
        self.assertEqual(first, service.render(self.config()))
        objects = first['manifests']
        self.assertTrue(all(o['metadata']['namespace'] == 'shared-web' for o in objects))
        self.assertFalse(any(o['kind'] in ('Secret', 'Namespace', 'Ingress', 'Role', 'ClusterRole') for o in objects))
        deploys = {o['metadata']['name']: o for o in objects if o['kind'] == 'Deployment'}
        self.assertEqual(set(deploys), {'firecrawl-api', 'firecrawl-playwright', 'firecrawl-redis',
                                      'firecrawl-rabbitmq', 'firecrawl-nuq-postgres'})
        for obj in deploys.values():
            pod = obj['spec']['template']['spec']
            self.assertFalse(pod['automountServiceAccountToken'])
            for container in pod['containers']:
                self.assertIn('@sha256:', container['image'])
                for env in container.get('env', []):
                    self.assertNotIn(env['name'], ['OPENAI_API_KEY', 'MODEL_NAME', 'OPENAI_BASE_URL'])
                    if 'valueFrom' in env:
                        self.assertEqual(env['valueFrom']['secretKeyRef']['name'], 'existing-queue')
        api = deploys['firecrawl-api']['spec']['template']['spec']['containers'][0]
        self.assertIn({'name': 'SEARXNG_ENDPOINT', 'value': 'http://search.search:8080'}, api['env'])
        self.assertEqual(api['readinessProbe']['httpGet']['path'], '/v0/health/readiness')
        policy = next(o for o in objects if o['kind'] == 'NetworkPolicy')
        self.assertEqual(policy['spec']['ingress'][1]['ports'][0]['port'], 3002)

    def opener(self, bodies):
        class Response:
            status = 200
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, count): return json.dumps(self.body).encode()
        mock = Mock()
        mock.open.side_effect = [Response(body) for body in bodies]
        return mock

    def test_functional_success(self):
        opener = self.opener([{}, {'success': True, 'data': {'web': [{'url': 'https://example.com'}]}},
                             {'success': True, 'data': {'markdown': 'Example Domain'}}])
        with patch.object(service.urllib.request, 'build_opener', return_value=opener):
            self.assertTrue(service.check('https://crawl.example.net', 'test-only')['success'])
        self.assertEqual(opener.open.call_count, 3)
        for call in opener.open.call_args_list:
            self.assertEqual(call.args[0].get_header('Authorization'), 'Bearer test-only')

    def test_empty_search_fails(self):
        opener = self.opener([{}, {'success': True, 'data': {'web': []}}])
        with patch.object(service.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(ValueError): service.check('https://crawl.example.net')

    def test_redirect_never_forwards_auth(self):
        self.assertIsNone(service.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example'))

    def test_bearer_requires_https(self):
        with self.assertRaises(ValueError): service.check('http://crawl.example.net', 'test-only')


if __name__ == '__main__':
    unittest.main()
