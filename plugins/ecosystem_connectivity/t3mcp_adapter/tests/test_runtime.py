"""Offline bootstrap tests using real JWT verification and fake transports."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from helpers_t3mcp import AS, PROJ
from t3mcp.runtime import RuntimeConfigError, build_runtime


class FakeClient:
    instances = []
    def __init__(self, url, token, *, timeout, session):
        self.url, self.token, self.timeout = url, token, timeout
        self.closed = False
        self.session = session
        self.instances.append(self)
    def close(self):
        self.closed = True
    def call_tool(self, name, arguments):
        self.last_call = (name, arguments)
        return SimpleNamespace(data={'environmentId': 'synthetic-environment'})


class FakeSession:
    def __init__(self):
        self.closed = False
        self.trust_env = True
    def close(self):
        self.closed = True


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session_patch = patch('requests.Session', FakeSession)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    def config(self):
        return {'resource': 'https://adapter.example/mcp', 'issuer': 'https://as.test/t3',
                'jwks': AS.jwks(), 'hosts': {'local': {'url': 'http://127.0.0.1:7777/mcp',
                                                    'credential_ref': 't3/local'}},
                'allowed_projects': [PROJ]}

    async def request(self, app, token, method='tools/list', params=None):
        sent = []
        async def receive():
            return {'type': 'http.request', 'body': json.dumps(
                {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}).encode()}
        async def send(msg):
            sent.append(msg)
        await app({'type': 'http', 'method': 'POST', 'path': '/mcp',
                   'headers': [(b'authorization', ('Bearer '+token).encode())]}, receive, send)
        return sent

    async def test_real_auth_read_only_and_separate_provider(self):
        calls = []
        def provider(ref):
            calls.append(ref)
            return 'synthetic-upstream-token'
        with patch('t3pipe.T3Client', FakeClient):
            app = build_runtime(self.config(), provider)
        try:
            token = AS.token(scopes=('t3:read',), audience='https://adapter.example/mcp')
            result = await self.request(app, token)
            self.assertEqual(result[0]['status'], 200)
            specs = json.loads(result[1]['body'])['result']['tools']
            self.assertFalse(any(x['name'].startswith(('t3_start','t3_send','t3_interrupt')) for x in specs))
            self.assertEqual(calls, ['t3/local'])
            self.assertFalse(FakeClient.instances[-1].session._session.trust_env)
            self.assertEqual(FakeClient.instances[-1].token, 'synthetic-upstream-token')
            self.assertEqual((await self.request(app, 'invalid'))[0]['status'], 401)
            self.assertEqual((await self.request(app, AS.token(audience='wrong')))[0]['status'], 401)
        finally:
            self.assertTrue(await app.aclose())
        self.assertTrue(FakeClient.instances[-1].session._session.closed)

    async def test_strict_configuration_rejects_before_provider(self):
        cases = []
        for field, value in [('allow_writes',True), ('allow_writes',0), ('resource','http://adapter.example/mcp'),
                             ('resource','https://adapter.example/wrong'), ('issuer','https://as.test/?secret=x'),
                             ('allowed_projects',['not-uuid']), ('limits',{'call_timeout_s':float('nan')}),
                             ('limits',{'read_page_limit':True}), ('limits',{'request_budget_s':10000}),
                             ('unexpected','value')]:
            c = self.config(); c[field]=value; cases.append(c)
        c=self.config(); c['hosts']['local']['url']='http://remote.example/mcp'; cases.append(c)
        c=self.config(); c['hosts']['local']['token']='secret'; cases.append(c)
        c=copy.deepcopy(self.config()); c['jwks']['keys'][0]['d']='private'; cases.append(c)
        for c in cases:
            with self.subTest(config_field=list(c)[-1]):
                with patch('t3pipe.T3Client', FakeClient), patch.object(self, 'provider', create=True) as provider:
                    with self.assertRaises(RuntimeConfigError): build_runtime(c, provider)
                    provider.assert_not_called()

    async def test_bad_public_key_fails_startup_before_provider(self):
        c=copy.deepcopy(self.config()); c['jwks']['keys'][0]['n']='AQ'
        with patch('t3pipe.T3Client', FakeClient), patch.object(self, 'provider', create=True) as provider:
            with self.assertRaises(RuntimeConfigError): build_runtime(c,provider)
            provider.assert_not_called()

    async def test_missing_provider_secret_is_sanitized_and_partial_clients_close(self):
        c=self.config(); c['hosts']['second']={'url':'https://upstream.example/mcp','credential_ref':'t3/second'}
        def provider(ref):
            if ref=='t3/second': raise RuntimeError('sensitive-value')
            return 'synthetic-token'
        with patch('t3pipe.T3Client', FakeClient):
            with self.assertRaises(RuntimeConfigError) as raised: build_runtime(c,provider)
        self.assertNotIn('sensitive-value',str(raised.exception))
        self.assertTrue(FakeClient.instances[-1].session._session.closed)

    async def test_blank_and_header_injection_credentials_fail(self):
        for token in ('',None,'x\r\nAuthorization: injected','x y'):
            with patch('t3pipe.T3Client', FakeClient):
                with self.assertRaises(RuntimeConfigError): build_runtime(self.config(),lambda ref:token)

    async def test_real_dispatch_uses_backend_and_denies_write_and_unknown_host(self):
        with patch('t3pipe.T3Client', FakeClient):
            app = build_runtime(self.config(), lambda ref: 'synthetic-upstream-token')
        try:
            token = AS.token(scopes=('t3:read','t3:write'), audience='https://adapter.example/mcp')
            result = await self.request(app,token,'tools/call',{'name':'t3_health','arguments':{}})
            data = json.loads(result[1]['body'])['result']['structuredContent']
            self.assertEqual(data['environment_id'],'synthetic-environment')
            self.assertFalse(data['write_enabled'])
            self.assertEqual(FakeClient.instances[-1].last_call,('t3_environment_read',{}))
            denied = await self.request(app,token,'tools/call',{'name':'t3_health','arguments':{'host':'unknown'}})
            self.assertIn(b'target_denied',denied[1]['body'])
            denied = await self.request(app,token,'tools/call',{'name':'t3_send_thread','arguments':{}})
            self.assertTrue(json.loads(denied[1]['body'])['result']['isError'])
        finally:
            await app.aclose()

    async def test_expired_and_missing_scope_tokens_fail(self):
        with patch('t3pipe.T3Client', FakeClient):
            app = build_runtime(self.config(), lambda ref: 'synthetic-token')
        try:
            expired = AS.token(expires_in=-60,audience='https://adapter.example/mcp')
            self.assertEqual((await self.request(app,expired))[0]['status'],401)
            no_scope = AS.token(scopes=(),audience='https://adapter.example/mcp')
            result = await self.request(app,no_scope,'tools/call',{'name':'t3_health','arguments':{}})
            self.assertEqual(result[0]['status'],403)
        finally:
            await app.aclose()

    async def test_weak_and_duplicate_keys_fail_before_provider(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from helpers_t3mcp import _b64url
        c=self.config(); c['jwks']['keys'].append(copy.deepcopy(c['jwks']['keys'][0]))
        weak=self.config(); n=rsa.generate_private_key(public_exponent=65537,key_size=1024).public_key().public_numbers().n
        weak['jwks']['keys'][0]['n']=_b64url(n.to_bytes((n.bit_length()+7)//8,'big'))
        for config in (c,weak):
            with patch.object(self,'provider',create=True) as provider:
                with self.assertRaises(RuntimeConfigError): build_runtime(config,provider)
                provider.assert_not_called()

    async def test_close_all_attempts_each_and_failed_session_can_retry(self):
        from t3mcp.runtime import RuntimeCloseError
        c=self.config(); c['hosts']['second']={'url':'https://upstream.example/mcp','credential_ref':'second'}
        with patch('t3pipe.T3Client',FakeClient):
            app=build_runtime(c,lambda ref:'synthetic-token')
        one,two=FakeClient.instances[-2:]
        with patch.object(one.session,'close',side_effect=RuntimeError('sensitive detail')) as close:
            with self.assertRaises(RuntimeCloseError) as raised: await app.aclose()
            self.assertNotIn('sensitive',str(raised.exception))
            self.assertTrue(two.session._session.closed)
            self.assertEqual(close.call_count,1)
        self.assertTrue(await app.aclose())
        self.assertTrue(one.session._session.closed)

    async def test_lifespan_cleanup_failure_is_explicit(self):
        with patch('t3pipe.T3Client',FakeClient):
            app=build_runtime(self.config(),lambda ref:'synthetic-token')
        outgoing=[]
        async def receive(): return {'type':'lifespan.shutdown'}
        async def send(message): outgoing.append(message)
        with patch.object(FakeClient.instances[-1].session,'close',side_effect=RuntimeError('secret')):
            await app({'type':'lifespan'},receive,send)
        self.assertEqual(outgoing[0]['type'],'lifespan.shutdown.failed')
        self.assertNotIn('secret',str(outgoing))
        await app.aclose()

    async def test_duplicate_jwt_members_and_critical_extensions_rejected(self):
        from helpers_t3mcp import _b64url
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        import base64
        original=AS.token(audience='https://adapter.example/mcp')
        encoded_header,encoded_claims,_=original.split('.')
        decode=lambda value:base64.urlsafe_b64decode(value+'='*(-len(value)%4)).decode()
        header,claims=decode(encoded_header),decode(encoded_claims)
        variants=[(header[:-1]+',"alg":"RS256"}',claims),
                  (header,claims[:-1]+',"sub":"other"}'),
                  (header[:-1]+',"crit":["unrecognized"],"unrecognized":true}',claims),
                  (header[:-1]+',"b64":true}',claims),
                  (json.dumps({'alg':'RS256','kid':[]}),claims),
                  (header,claims[:-1]+',"extra":NaN}')]
        missing_scope=json.loads(claims); del missing_scope['scope']
        variants.append((header,json.dumps(missing_scope)))
        with patch('t3pipe.T3Client',FakeClient):
            app=build_runtime(self.config(),lambda ref:'synthetic-token')
        try:
            for h,c in variants:
                payload=_b64url(h.encode())+'.'+_b64url(c.encode())
                signature=AS._key.sign(payload.encode(),padding.PKCS1v15(),hashes.SHA256())
                token=payload+'.'+_b64url(signature)
                self.assertEqual((await self.request(app,token))[0]['status'],401)
        finally:
            await app.aclose()


    async def test_real_client_through_bounded_fake_http_transport(self):
        calls=[]
        class WireSession(FakeSession):
            def post(self,url,*,json,headers,timeout,allow_redirects,stream):
                calls.append((json['method'],headers['Authorization'],stream))
                if json['method']=='initialize':
                    result={'protocolVersion':'2025-06-18'}
                elif json['method']=='notifications/initialized':
                    return BoundedTransportTests().response([],status=202)
                else:
                    result={'structuredContent':{'environmentId':'wire-test'},'content':[]}
                payload=__import__('json').dumps({'jsonrpc':'2.0','id':json['id'],'result':result}).encode()
                return BoundedTransportTests().response([payload])
        with patch('requests.Session',WireSession):
            app=build_runtime(self.config(),lambda ref:'synthetic-upstream-token')
        try:
            token=AS.token(audience='https://adapter.example/mcp')
            result=await self.request(app,token,'tools/call',{'name':'t3_health','arguments':{}})
            data=json.loads(result[1]['body'])['result']['structuredContent']
            self.assertEqual(data['environment_id'],'wire-test')
            self.assertEqual([c[0] for c in calls],['initialize','notifications/initialized','tools/call'])
            self.assertTrue(all(c[1]=='Bearer synthetic-upstream-token' and c[2] for c in calls))
        finally:
            await app.aclose()


class BoundedTransportTests(unittest.TestCase):
    def response(self,chunks,headers=None,status=200):
        class Response:
            status_code=status
            closed=False
            def iter_content(self,chunk_size):
                yield from chunks
            def close(self): self.closed=True
        result=Response(); result.headers=headers or {}
        return result

    def session(self,response):
        from t3mcp.runtime import _BoundedSession
        underlying=unittest.mock.Mock()
        underlying.post.return_value=response
        return _BoundedSession(underlying),underlying

    def call(self,session):
        return session.post('https://upstream.example/mcp',json={},headers={},timeout=1,allow_redirects=False)

    def test_streams_without_unbounded_text_property_and_closes(self):
        response=self.response([b'{"jsonrpc":',b'"2.0"}'])
        session,underlying=self.session(response)
        self.assertEqual(json.loads(self.call(session).text)['jsonrpc'],'2.0')
        self.assertTrue(response.closed)
        self.assertTrue(underlying.post.call_args.kwargs['stream'])
        self.assertFalse(underlying.trust_env)

    def test_declared_and_actual_limits_and_bad_utf8_fail_closed(self):
        from t3pipe.errors import ProtocolError
        for response in (self.response([],{ 'Content-Length':'1000001'}),
                         self.response([b'x'*600000,b'y'*500000]),
                         self.response([b'\xff'])):
            session,_=self.session(response)
            with self.assertRaises(ProtocolError): self.call(session)
            self.assertTrue(response.closed)

    def test_stream_exception_still_closes_response(self):
        def broken():
            yield b'x'
            raise ConnectionError('fake stream failure')
        response=self.response(broken())
        session,_=self.session(response)
        with self.assertRaises(ConnectionError): self.call(session)
        self.assertTrue(response.closed)

    def test_error_status_never_reads_body(self):
        def forbidden():
            raise AssertionError('error body must not be read')
            yield
        response=self.response(forbidden(),status=401)
        session,_=self.session(response)
        self.assertEqual(self.call(session).status_code,401)
        self.assertTrue(response.closed)

    def test_stream_error_and_budget_expiry_close_response(self):
        from t3pipe.errors import ProtocolError
        response=self.response([b'x'])
        session,_=self.session(response)
        with patch('t3mcp.runtime.time.monotonic',side_effect=[0,2]):
            with self.assertRaises(ProtocolError): self.call(session)
        self.assertTrue(response.closed)
