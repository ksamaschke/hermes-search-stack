#!/usr/bin/env python3
"""Render a shared Firecrawl GitOps install or verify an existing endpoint.

Rendering never contacts the endpoint or writes to Kubernetes. PyYAML and
kubectl (local Kustomize only) are required for managed-install rendering.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('endpoint must be an HTTP(S) URL without credentials, query or fragment')
    return value.rstrip('/')


def name(value):
    if not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?', value):
        raise ValueError('expected a Kubernetes DNS label')
    return value


def render(config):
    mode = config['mode']
    endpoint = url(config['endpoint'])
    if config.get('auth') not in ('network-restricted', 'bearer-proxy'):
        raise ValueError('auth must explicitly name network-restricted or bearer-proxy')
    contract = {'mode': mode, 'endpoint': endpoint, 'auth': config['auth'],
                'readiness_path': '/v0/health/readiness',
                'search_path': '/v2/search', 'scrape_path': '/v2/scrape'}
    if mode == 'existing-service':
        return {'contract': contract, 'manifests': []}
    if mode != 'managed-install':
        raise ValueError('mode must be existing-service or managed-install; no auto detection')
    import yaml
    namespace = name(config['namespace'])
    secret = name(config['postgres_secret'])
    ingress_namespace = name(config['ingress_namespace'])
    searxng = url(config['searxng_endpoint'])
    if endpoint.startswith('http:'):
        raise ValueError('managed external endpoint requires HTTPS at the authorized routing layer')
    builder = ['kustomize', 'build'] if shutil.which('kustomize') else ['kubectl', 'kustomize']
    rendered = subprocess.run(builder + [str(ROOT / 'deploy/kubernetes/base/firecrawl')],
                              check=True, capture_output=True, text=True).stdout
    manifests = list(yaml.safe_load_all(rendered))
    for obj in manifests:
        obj['metadata']['namespace'] = namespace
        if obj['kind'] != 'Deployment':
            continue
        pod = obj['spec']['template']['spec']
        pod['automountServiceAccountToken'] = False
        for container in pod['containers']:
            env = container.get('env', [])
            # Basic search/scrape does not need a model, Hermes config or model key.
            env[:] = [e for e in env if e['name'] not in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'MODEL_NAME')]
            for entry in env:
                if entry['name'] == 'SEARXNG_ENDPOINT':
                    entry['value'] = searxng
                ref = entry.get('valueFrom', {}).get('secretKeyRef')
                if ref:
                    ref['name'] = secret
            if obj['metadata']['name'] == 'firecrawl-api':
                container['readinessProbe']['httpGet']['path'] = '/v0/health/readiness'
    # Dependencies are private to this dedicated shared-service namespace.
    # Only the pre-existing authorized ingress/proxy namespace may reach the API.
    manifests.append({'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
        'metadata': {'name': 'firecrawl-shared-ingress', 'namespace': namespace},
        'spec': {'podSelector': {}, 'policyTypes': ['Ingress'], 'ingress': [
            {'from': [{'podSelector': {}}]},
            {'from': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': ingress_namespace}}}],
             'ports': [{'protocol': 'TCP', 'port': 3002}]}]}})
    contract.update({'namespace': namespace, 'service': 'firecrawl-api', 'port': 3002,
                     'postgres_secret': secret, 'searxng_endpoint': searxng,
                     'routing': 'existing authorized HTTPS proxy must route to firecrawl-api:3002'})
    return {'contract': contract, 'manifests': copy.deepcopy(manifests)}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward bearer credentials to a redirect destination.
        return None


def check(endpoint, token=None, timeout=45):
    endpoint = url(endpoint)
    if token and not endpoint.startswith('https://'):
        raise ValueError('bearer authentication requires HTTPS')
    opener = urllib.request.build_opener(NoRedirect)
    results = []
    requests = [('/v0/health/readiness', None),
                ('/v2/search', {'query': 'IANA example domain', 'limit': 2}),
                ('/v2/scrape', {'url': 'https://example.com', 'formats': ['markdown'], 'timeout': 30000})]
    for path, payload in requests:
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request(endpoint + path, headers=headers,
                    data=json.dumps(payload).encode() if payload is not None else None)
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('response exceeds check size limit')
            status = response.status
        if status != 200:
            raise ValueError('endpoint did not return HTTP 200')
        result = {'path': path, 'http_status': status}
        if payload is not None:
            data = json.loads(raw)
            if data.get('success') is not True:
                raise ValueError('functional request did not succeed')
            content = data.get('data')
            if path.endswith('/search'):
                hits = content.get('web', []) if isinstance(content, dict) else content
                if not isinstance(hits, list) or not hits or not all(isinstance(h, dict) and h.get('url') for h in hits):
                    raise ValueError('search returned no URL results; verify SearXNG JSON search')
                result['results'] = len(hits)
            else:
                markdown = content.get('markdown', '') if isinstance(content, dict) else ''
                expected = ('example domain', 'this domain is for use in documentation examples')
                if not any(text in markdown.lower() for text in expected):
                    raise ValueError('scrape did not contain expected example-domain documentation text')
                result['markdown_chars'] = len(markdown)
        results.append(result)
    return {'checks': results, 'success': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('render')
    p.add_argument('config', type=Path, help='non-secret JSON input')
    p = sub.add_parser('check')
    p.add_argument('--endpoint', required=True)
    p.add_argument('--token-env', help='existing proxy bearer token environment variable name, never the token')
    args = parser.parse_args()
    try:
        if args.command == 'render':
            result = render(json.loads(args.config.read_text()))
        else:
            token = os.environ[args.token_env] if args.token_env else None
            result = check(args.endpoint, token)
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as error:
        # URLs, response bodies and exception text can contain credentials.
        print(json.dumps({'success': False, 'error_type': type(error).__name__,
                          'action': 'verify explicit inputs, endpoint route/auth and dependencies; never auto-install'}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
