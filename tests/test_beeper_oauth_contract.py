"""In-process resource-server contract, not an authorization-server simulation."""
import copy
import json
import re
import unittest
from urllib.parse import urlsplit
from unittest.mock import Mock, patch

import test_beeper_http as helpers
from plugins.ecosystem_connectivity.beeper_mcp_plugin import BeeperMCPPlugin


class OAuthContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        helpers.HTTPTests.setUpClass()

    def setUp(self):
        self.h = helpers.HTTPTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.config = copy.deepcopy(self.h.config)
        # Path-bearing issuer and resource must remain exact, not origin-only.
        self.h.config['issuer'] = 'https://issuer.example.invalid/tenant/alice'
        self.h.config['resource'] = 'https://mcp.example.invalid/private/beeper'
        self.provider = Mock(return_value='synthetic-independent-backend-token')
        self.observed = []
        observed = self.observed

        class FakeBackend:
            def __init__(self, *args, **kwargs): pass
            def notify(self, payload, token): observed.append((payload, token))
            def post(self, payload, token):
                observed.append((payload, token))
                if payload['method'] == 'initialize':
                    result = {'protocolVersion': '2025-03-26'}
                elif payload['method'] == 'tools/list':
                    result = {'tools': BeeperMCPPlugin._tools()}
                else:
                    result = {'content': [{'type': 'text', 'text': json.dumps(
                        {'chat_id': 'a', 'messages': []})}]}
                return {'jsonrpc': '2.0', 'id': payload['id'], 'result': result}

        self.transport = patch('plugins.ecosystem_connectivity.beeper_bootstrap.StreamableHTTPTransport', FakeBackend)
        self.transport.start()
        self.addCleanup(self.transport.stop)
        self.h.app = self.h.build(provider=self.provider)
        self.payload = {'jsonrpc': '2.0', 'id': 42, 'method': 'tools/call',
                        'params': {'name': 'list_messages', 'arguments': {'chatID': 'a'}}}

    def call(self, payload=None, token=None, **kwargs):
        kwargs.setdefault('PATH_INFO', '/private/beeper')
        return self.h.call(payload, token, **kwargs)

    def discovery(self, challenge):
        # Extract only the server-produced advertised metadata reference.
        match = re.search(r'resource_metadata="([^"]+)"', challenge)
        self.assertIsNotNone(match)
        url = urlsplit(match.group(1))
        self.assertEqual((url.scheme, url.netloc), ('https', 'mcp.example.invalid'))
        self.assertEqual(url.path, '/.well-known/oauth-protected-resource/private/beeper')
        status, _, metadata = self.call(REQUEST_METHOD='GET', PATH_INFO=url.path)
        self.assertEqual(status, 200)
        self.assertEqual(metadata['resource'], self.h.config['resource'])
        self.assertEqual(metadata['authorization_servers'], [self.h.config['issuer']])
        self.assertEqual(metadata['scopes_supported'], ['beeper:read'])
        self.assertEqual(metadata['bearer_methods_supported'], ['header'])
        return metadata

    def test_discovery_scope_upgrade_and_expired_token_retry(self):
        status, headers, _ = self.call(self.payload)
        self.assertEqual(status, 401)
        self.assertNotIn('error=', headers['WWW-Authenticate'])
        metadata = self.discovery(headers['WWW-Authenticate'])
        self.provider.assert_not_called()
        self.assertFalse(self.observed)

        status, headers, _ = self.call(self.payload, self.h.token(scope='beeper:write'))
        self.assertEqual(status, 403)
        self.assertIn('error="insufficient_scope"', headers['WWW-Authenticate'])
        self.assertIn('scope="beeper:read"', headers['WWW-Authenticate'])
        self.assertEqual(self.discovery(headers['WWW-Authenticate']), metadata)
        self.provider.assert_not_called()
        self.assertFalse(self.observed)

        status, headers, _ = self.call(self.payload, self.h.token(exp=1))
        self.assertEqual(status, 401)
        self.assertIn('error="invalid_token"', headers['WWW-Authenticate'])
        self.assertEqual(self.discovery(headers['WWW-Authenticate']), metadata)
        self.provider.assert_not_called()
        self.assertFalse(self.observed)

        # Locally signed fixtures test resource verification, not OAuth issuance.
        renewed = self.h.token(aud=metadata['resource'], scope=' '.join(metadata['scopes_supported']))
        status, _, listed = self.call(token=renewed)
        self.assertEqual(status, 200)
        for tool in listed['result']['tools']:
            self.assertEqual(tool['securitySchemes'], [{'type': 'oauth2', 'scopes': metadata['scopes_supported']}])
            self.assertEqual(tool['_meta']['securitySchemes'], tool['securitySchemes'])
        self.provider.assert_not_called()
        status, _, result = self.call(self.payload, renewed)
        self.assertEqual(status, 200)
        self.assertEqual(result['id'], 42)
        self.assertIn('result', result)
        self.provider.assert_called_once()
        self.assertEqual([p['method'] for p, _ in self.observed],
                         ['initialize', 'notifications/initialized', 'tools/list', 'tools/call'])
        self.assertEqual({t for _, t in self.observed}, {'synthetic-independent-backend-token'})
        self.assertNotIn(renewed, [t for _, t in self.observed])

    def test_origin_only_claims_and_renewed_wrong_chat_remain_denied(self):
        for claims in ({'aud': 'https://mcp.example.invalid'},
                       {'iss': 'https://issuer.example.invalid'}):
            status, headers, _ = self.call(self.payload, self.h.token(**claims))
            self.assertEqual(status, 401)
            self.assertIn('error="invalid_token"', headers['WWW-Authenticate'])
            self.discovery(headers['WWW-Authenticate'])
        status, headers, _ = self.call(self.payload, self.h.token(sub='bob'))
        self.assertEqual(status, 403)
        self.assertNotIn('WWW-Authenticate', headers)
        self.provider.assert_not_called()
        self.assertFalse(self.observed)


if __name__ == '__main__': unittest.main()
