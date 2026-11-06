"""Offline shared-verifier regressions. Keys and tokens are process-local only."""
import base64
import hashlib
import hmac
import json
import os
import socket
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from plugins.ecosystem_connectivity.t3mcp_adapter.t3mcp.auth import JwtVerifier
from plugins.ecosystem_connectivity.t3mcp_adapter.t3mcp.errors import AdapterError
from plugins.ecosystem_connectivity.beeper_bootstrap import create_secure_readonly_beeper_app


def encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def jwks(key):
    numbers = key.public_key().public_numbers()
    def integer(n):
        return encode(n.to_bytes((n.bit_length() + 7) // 8, 'big'))
    return {'keys': [{'kty': 'RSA', 'kid': 'test', 'n': integer(numbers.n), 'e': integer(numbers.e)}]}


class JwtBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)

    def setUp(self):
        blocker = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        blocker.start()
        self.addCleanup(blocker.stop)

    def verifier(self, audience='https://resource.invalid/mcp', **kwargs):
        return JwtVerifier(issuer='https://issuer.invalid', audience=audience,
                           algorithm='RS256', jwks=jwks(self.key),
                           wall_clock=lambda: 1000, **kwargs)

    def token(self, claims=None, header=None, key=None):
        claims = {'iss': 'https://issuer.invalid', 'aud': 'https://resource.invalid/mcp',
                  'exp': 1100, 'sub': 'alice', 'scope': 'beeper:read', **(claims or {})}
        header = {'alg': 'RS256', 'kid': 'test', **(header or {})}
        signed = (encode(json.dumps(header).encode()) + '.' + encode(json.dumps(claims).encode())).encode()
        signature = (key or self.key).sign(signed, padding.PKCS1v15(), hashes.SHA256())
        return signed.decode() + '.' + encode(signature)

    def test_weak_rsa_rejected_at_verifier_and_beeper_construction(self):
        with self.assertRaisesRegex(AdapterError, 'at least 2048'):
            JwtVerifier(issuer='https://issuer.invalid', audience='https://resource.invalid/mcp',
                        algorithm='RS256', jwks=jwks(self.weak))
        config = {'issuer': 'https://issuer.invalid', 'resource': 'https://resource.invalid/mcp',
                  'jwks': jwks(self.weak), 'chat_entitlements': {'alice': ['chat-a']},
                  'beeper_url': 'http://127.0.0.1:23373/v0/mcp'}
        with self.assertRaisesRegex(AdapterError, 'at least 2048'):
            create_secure_readonly_beeper_app(config, beeper_token_provider=lambda caller: 'synthetic')

    def test_string_and_string_array_audiences_remain_supported(self):
        for audience in ('https://resource.invalid/mcp', 'urn:t3mcp:test'):
            verifier = self.verifier(audience)
            for claim in (audience, [audience], ['another-resource', audience]):
                with self.subTest(audience=audience, claim=claim):
                    self.assertEqual(verifier.verify(self.token({'aud': claim})).subject, 'alice')

    def test_malformed_audience_arrays_rejected_even_with_expected_member(self):
        verifier = self.verifier()
        for member in (None, True, 123, {}, [], ['nested']):
            with self.subTest(member=member), self.assertRaises(AdapterError):
                verifier.verify(self.token({'aud': [verifier.audience, member]}))
        for audience in ([], None, 123, {}, ['wrong']):
            with self.subTest(audience=audience), self.assertRaises(AdapterError):
                verifier.verify(self.token({'aud': audience}))

    def test_algorithm_issuer_and_key_selection_stay_pinned(self):
        verifier = self.verifier()
        for header in ({'alg': 'none'}, {'alg': 'HS256'}, {'kid': 'unknown'}, {'crit': ['x']}):
            with self.subTest(header=header), self.assertRaises(AdapterError):
                verifier.verify(self.token(header=header))
        with self.assertRaises(AdapterError):
            verifier.verify(self.token({'iss': 'https://other.invalid'}))
        # Token-supplied key URLs/keys cannot replace the configured signing key.
        with self.assertRaises(AdapterError):
            verifier.verify(self.token(key=self.weak, header={
                'jwk': jwks(self.weak)['keys'][0], 'jku': 'https://other.invalid/jwks'}))

    def test_existing_leeway_policy_unchanged(self):
        verifier = self.verifier()
        for exp in (1001, 1000, 971, 970):
            self.assertEqual(verifier.verify(self.token({'exp': exp})).subject, 'alice')
        with self.assertRaises(AdapterError):
            verifier.verify(self.token({'exp': 969}))
        with self.assertRaises(AdapterError):
            self.verifier(leeway_s=0).verify(self.token({'exp': 999}))
        self.assertEqual(verifier.verify(self.token({'nbf': 1030})).subject, 'alice')
        with self.assertRaises(AdapterError):
            verifier.verify(self.token({'nbf': 1031}))

    def test_shared_hs256_audience_validation_remains_consistent(self):
        # The generic verifier retains its existing explicit HS256 mode;
        # neither composed production bootstrap opts into that mode.
        secret = 'synthetic-test-only-secret-0123456789'
        with patch.dict(os.environ, {'JWT_BOUNDARY_TEST_SECRET': secret}):
            verifier = JwtVerifier(issuer='https://issuer.invalid', audience='urn:t3mcp:test',
                algorithm='HS256', secret_env='JWT_BOUNDARY_TEST_SECRET', wall_clock=lambda: 1000)
        for audience, valid in [('urn:t3mcp:test', True), (['urn:t3mcp:test'], True),
                                (['urn:t3mcp:test', False], False)]:
            claims = {'iss': verifier.issuer, 'aud': audience, 'sub': 'alice',
                      'scope': 't3:read', 'exp': 1100}
            signed = (encode(b'{"alg":"HS256"}') + '.' + encode(json.dumps(claims).encode())).encode()
            token = signed.decode() + '.' + encode(hmac.new(secret.encode(), signed, hashlib.sha256).digest())
            if valid:
                self.assertEqual(verifier.verify(token).subject, 'alice')
            else:
                with self.assertRaises(AdapterError):
                    verifier.verify(token)


if __name__ == '__main__':
    unittest.main()
