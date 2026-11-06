import base64
import copy
import io
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from plugins.ecosystem_connectivity.beeper_bootstrap import create_secure_readonly_beeper_app
from plugins.ecosystem_connectivity.beeper_mcp_plugin import BeeperMCPPlugin


def segment(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=')


def integer(value):
    return base64.urlsafe_b64encode(value.to_bytes((value.bit_length()+7)//8,'big')).rstrip(b'=').decode()


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        pub=cls.key.public_key().public_numbers()
        cls.config={'resource':'https://mcp.example.invalid/beeper','issuer':'https://issuer.example.invalid',
            'jwks':{'keys':[{'kty':'RSA','kid':'test','n':integer(pub.n),'e':integer(pub.e)}]},
            'chat_entitlements':{'alice':['a'],'bob':['b']},'beeper_url':'http://127.0.0.1:12345/mcp'}

    def setUp(self):
        self.socket_patch=patch.object(socket,'socket',side_effect=AssertionError('network forbidden'))
        self.socket_patch.start();self.addCleanup(self.socket_patch.stop)
        self.app=self.build()

    def build(self,provider=None,**limits):
        return create_secure_readonly_beeper_app(self.config,
            beeper_token_provider=provider or (lambda caller:'backend-'+caller.subject),**limits)

    def token(self,**claims):
        payload={'iss':self.config['issuer'],'aud':self.config['resource'],'sub':'alice',
                 'exp':int(time.time())+60,'scope':'beeper:read'}
        payload.update(claims)
        signed=segment({'alg':'RS256','kid':'test'})+b'.'+segment(payload)
        signature=self.key.sign(signed,padding.PKCS1v15(),hashes.SHA256())
        return (signed+b'.'+base64.urlsafe_b64encode(signature).rstrip(b'=')).decode()

    def call(self,payload=None,token=None,app=None,raw=None,**updates):
        if raw is None:raw=json.dumps(payload or {'jsonrpc':'2.0','id':1,'method':'tools/list'}).encode()
        env={'wsgi.url_scheme':'https','HTTP_HOST':'mcp.example.invalid','REQUEST_METHOD':'POST',
             'PATH_INFO':'/beeper','CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(raw)),
             'HTTP_ACCEPT':'application/json, text/event-stream','wsgi.input':io.BytesIO(raw)}
        if token:env['HTTP_AUTHORIZATION']='Bearer '+token
        env.update(updates); captured=[]
        body=b''.join((app or self.app)(env,lambda status,headers:captured.append((status,dict(headers)))))
        status,headers=captured[0]
        return int(status.split()[0]),headers,json.loads(body) if body else None

    def test_metadata_without_reading_body(self):
        class Unreadable:
            def read(self,*args):raise AssertionError('metadata read body')
        status,headers,data=self.call(REQUEST_METHOD='GET',PATH_INFO='/.well-known/oauth-protected-resource/beeper',
                                      **{'wsgi.input':Unreadable()})
        self.assertEqual(status,200);self.assertEqual(data['resource'],self.config['resource'])
        self.assertEqual(data['authorization_servers'],[self.config['issuer']])
        self.assertEqual(data['scopes_supported'],['beeper:read'])

    def test_bearer_challenges(self):
        status,headers,_=self.call();self.assertEqual(status,401)
        self.assertEqual(headers['WWW-Authenticate'],'Bearer resource_metadata="https://mcp.example.invalid/.well-known/oauth-protected-resource/beeper"')
        for token in ['invalid',self.token(aud='https://wrong.invalid'),self.token(iss='https://wrong.invalid'),self.token(exp=1)]:
            status,headers,_=self.call(token=token)
            self.assertEqual(status,401);self.assertIn('error="invalid_token"',headers['WWW-Authenticate'])

    def test_scope_and_entitlement_separation(self):
        status,headers,_=self.call(token=self.token(scope='other:read'))
        self.assertEqual(status,403);self.assertIn('error="insufficient_scope"',headers['WWW-Authenticate'])
        self.assertIn('scope="beeper:read"',headers['WWW-Authenticate'])
        status,headers,_=self.call(token=self.token(sub='unknown'))
        self.assertEqual(status,403);self.assertNotIn('WWW-Authenticate',headers)

    def test_initialize_tools_and_notification(self):
        token=self.token()
        status,_,data=self.call({'jsonrpc':'2.0','id':1,'method':'initialize','params':{
            'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'test','version':'1'}}},token)
        self.assertEqual(status,200);self.assertEqual(data['result']['protocolVersion'],'2025-03-26')
        status,_,data=self.call(token=token);self.assertEqual(status,200)
        self.assertEqual({t['name'] for t in data['result']['tools']},{'search_chats','list_messages'})
        status,_,data=self.call({'jsonrpc':'2.0','method':'notifications/initialized'},token)
        self.assertEqual((status,data),(202,None))

    def test_signed_overflow_number_is_rejected(self):
        for number in ('1e9999', '-1e9999'):
            raw=('{"jsonrpc":"2.0","id":1,"method":"tools/list",'
                 '"params":{"nested":['+number+']}}').encode()
            with self.subTest(number=number):
                self.assertEqual(self.call(raw=raw,token=self.token())[0],400)

    def test_notifications_use_http_rejection_not_rpc_response(self):
        for notification in [
            {'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':1}},
            {'jsonrpc':'2.0','method':'notifications/unknown'},
            {'jsonrpc':'2.0','method':'notifications/initialized','id':1},
            {'jsonrpc':'2.0','method':'notifications/initialized','params':[]},
            {'method':'notifications/initialized'},
        ]:
            with self.subTest(notification=notification):
                status,_,body=self.call(notification,self.token())
                self.assertEqual(status,400)
                self.assertNotIn('jsonrpc',body)
                self.assertNotIn('id',body)
        status,_,body=self.call({'jsonrpc':'2.0','method':'notifications/initialized'},self.token())
        self.assertEqual((status,body),(202,None))

    def test_routing_origin_and_framing(self):
        cases=[({'HTTP_HOST':'evil.invalid'},421),({'wsgi.url_scheme':'http'},421),
            ({'PATH_INFO':'/other'},404),({'QUERY_STRING':'access_token=x'},404),
            ({'HTTP_ORIGIN':'https://evil.invalid'},403),({'REQUEST_METHOD':'GET'},405),
            ({'HTTP_TRANSFER_ENCODING':'chunked'},400),({'CONTENT_LENGTH':'-1'},400),
            ({'CONTENT_LENGTH':'1,1'},400),({'CONTENT_LENGTH':'99999999'},413),
            ({'CONTENT_TYPE':'text/plain'},415),({'HTTP_ACCEPT':'application/json'},406),
            ({'HTTP_MCP_PROTOCOL_VERSION':'bad'},400),({'HTTP_X_TEST':'x\r\ny'},400)]
        for updates,expected in cases:
            with self.subTest(updates=updates):self.assertEqual(self.call(token=self.token(),**updates)[0],expected)

    def test_json_and_body_negatives(self):
        for raw in [b'{"id":1,"id":2}',b'{"value":NaN}',b'[]',b'\xff',b'{']:
            with self.subTest(raw=raw):self.assertEqual(self.call(raw=raw)[0],400)
        self.assertEqual(self.call(CONTENT_LENGTH='999')[0],400)
        self.assertEqual(self.call({'jsonrpc':'2.0','id':1,'method':'initialize'})[0],400)

    def test_header_and_response_bounds(self):
        with self.assertRaises(ValueError):self.build(max_response_bytes=1)
        self.assertEqual(self.call(app=self.build(max_header_bytes=5))[0],431)
        self.assertEqual(self.call(token=self.token(),app=self.build(max_response_bytes=100))[0],502)

    def test_allowed_origin(self):
        self.assertEqual(self.call(token=self.token(),app=self.build(allowed_origins=['https://client.example.invalid']),
                                  HTTP_ORIGIN='https://client.example.invalid')[0],200)

    def test_backend_flow_and_token_nonforwarding(self):
        observations=[]
        class FakeTransport:
            def __init__(self,*args,**kwargs):pass
            def notify(self,payload,token):observations.append((token,payload['method']))
            def post(self,payload,token):
                observations.append((token,payload['method']))
                if payload['method']=='initialize':result={'protocolVersion':'2025-03-26'}
                elif payload['method']=='tools/list':result={'tools':BeeperMCPPlugin._tools()}
                else:
                    chat=payload['params']['arguments']['chatID']
                    result={'content':[{'type':'text','text':json.dumps({'chat_id':chat,'messages':[]})}]}
                return {'jsonrpc':'2.0','id':payload['id'],'result':result}
        with patch('plugins.ecosystem_connectivity.beeper_bootstrap.StreamableHTTPTransport',FakeTransport):
            app=self.build()
            for subject,chat in [('alice','a'),('bob','b')]:
                payload={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'list_messages','arguments':{'chatID':chat}}}
                status,_,data=self.call(payload,self.token(sub=subject),app)
                self.assertEqual(status,200);self.assertIn('result',data)
                self.assertEqual({token for token,method in observations[-4:]},{'backend-'+subject})
            count=len(observations)
            self.assertEqual(self.call(payload,self.token(sub='alice'),app)[0],403)
            self.assertEqual(len(observations),count)
            caller_token=self.token(sub='bob')
            app=self.build(provider=lambda caller:caller_token)
            status,_,data=self.call(payload,caller_token,app)
            self.assertEqual(data['error']['message'],'backend unavailable')
            self.assertEqual(len(observations),count)

    def test_busy_admission_and_release(self):
        entered=threading.Event();release=threading.Event()
        class BlockingBody(io.BytesIO):
            def read(self,n):entered.set();release.wait(2);return super().read(n)
        raw=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list'}).encode()
        result=[]
        worker=threading.Thread(target=lambda:result.append(self.call(token=self.token(),raw=raw,
                                      **{'wsgi.input':BlockingBody(raw)})))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.call(token=self.token())[0],503)
        finally:release.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertEqual(result[0][0],200)
        self.assertEqual(self.call(token=self.token())[0],200)

if __name__=='__main__':unittest.main()
