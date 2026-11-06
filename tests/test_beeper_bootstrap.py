import base64
import copy
import io
import json
import socket
import time
import unittest
import urllib.request
import urllib.response
from unittest.mock import patch
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from plugins.ecosystem_connectivity.beeper_bootstrap import create_readonly_beeper_app


def b64int(n):
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length()+7)//8, 'big')).rstrip(b'=').decode()


class BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key = cls.private_key.public_key().public_numbers()
        cls.config = dict(resource='https://mcp.example.invalid/beeper', issuer='https://issuer.example.invalid',
            jwks={'keys':[{'kty':'RSA','kid':'test','n':b64int(key.n),'e':b64int(key.e)}]},
            chat_entitlements={'alice':['chat-a']}, beeper_url='http://127.0.0.1:12345/mcp')

    def build(self, config):
        return create_readonly_beeper_app(config, beeper_token_provider=lambda caller: self.fail('provider called during startup/auth rejection'))

    def test_build_and_unauthenticated_request_never_connect(self):
        with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')):
            app = self.build(self.config)
            body = json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list'}).encode()
            statuses=[]
            result = app({'REQUEST_METHOD':'POST','CONTENT_TYPE':'application/json',
                'CONTENT_LENGTH':str(len(body)),'wsgi.input':io.BytesIO(body)},
                lambda status, headers: statuses.append(status))
            self.assertTrue(statuses[0].startswith('401'))
            self.assertIn(b'unauthorized', b''.join(result))

    def test_real_signed_caller_can_list_only_read_tools(self):
        def segment(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=')
        signed = segment({'alg':'RS256','kid':'test'}) + b'.' + segment({
            'iss':self.config['issuer'], 'aud':self.config['resource'],
            'sub':'alice', 'exp':int(time.time())+60, 'scope':'beeper:read'})
        signature = self.private_key.sign(signed,padding.PKCS1v15(),hashes.SHA256())
        token=(signed+b'.'+base64.urlsafe_b64encode(signature).rstrip(b'=')).decode()
        with patch.object(socket,'socket',side_effect=AssertionError('network forbidden')):
            app=self.build(self.config)
            body=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list'}).encode()
            statuses=[]
            result=app({'REQUEST_METHOD':'POST','CONTENT_TYPE':'application/json',
                'CONTENT_LENGTH':str(len(body)), 'wsgi.input':io.BytesIO(body),
                'HTTP_AUTHORIZATION':'Bearer '+token},lambda status,headers:statuses.append(status))
            self.assertTrue(statuses[0].startswith('200'))
            names={tool['name'] for tool in json.loads(b''.join(result))['result']['tools']}
            self.assertEqual(names,{'search_chats','list_messages'})

    def test_backend_reads_use_distinct_credentials_and_entitlements(self):
        from plugins.ecosystem_connectivity.beeper_mcp_plugin import BeeperMCPPlugin
        observed=[]
        backend_chats={'Bearer backend-alice':('chat-a','canonical-a'),
                       'Bearer backend-bob':('chat-b','canonical-b')}
        def fake_http_open(handler, request):
            authorization=request.get_header('Authorization')
            self.assertIn(authorization,backend_chats)
            local,canonical=backend_chats[authorization]
            if request.get_method()=='GET':
                self.assertIsNone(request.data)
                self.assertEqual(request.full_url,
                    'http://127.0.0.1:12345/v1/chats/'+local+'?maxParticipantCount=0')
                observed.append((authorization,'resolve_chat_identity'))
                body=json.dumps({'localChatID':local,'id':canonical,
                    'title':'unprojected synthetic metadata'}).encode()
                response=urllib.response.addinfourl(io.BytesIO(body),{},request.full_url,200)
                response.msg='OK'
                return response
            self.assertEqual(request.get_method(),'POST')
            payload=json.loads(request.data)
            observed.append((authorization,payload['method']))
            method=payload['method']
            if method=='initialize': result={'protocolVersion':'2025-03-26'}
            elif method=='tools/list': result={'tools':BeeperMCPPlugin._tools()}
            elif method=='tools/call':
                chat=payload['params']['arguments']['chatID']
                self.assertEqual(chat,canonical)
                result={'content':[{'type':'text','text':json.dumps({
                    'items':[{'id':'synthetic','chatID':chat,'text':'synthetic body'}],
                    'hasMore':False,'oldestCursor':None,'newestCursor':None})}]}
            else: result={}
            body=json.dumps({'jsonrpc':'2.0','id':payload.get('id'),'result':result}).encode()
            response=urllib.response.addinfourl(io.BytesIO(body),{},request.full_url,200)
            response.msg='OK'
            return response
        def token_for(subject):
            def segment(value):return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=')
            signed=segment({'alg':'RS256','kid':'test'})+b'.'+segment({
                'iss':self.config['issuer'],'aud':self.config['resource'],'sub':subject,
                'exp':int(time.time())+60,'scope':'beeper:read'})
            signature=self.private_key.sign(signed,padding.PKCS1v15(),hashes.SHA256())
            return (signed+b'.'+base64.urlsafe_b64encode(signature).rstrip(b'=')).decode()
        config=copy.deepcopy(self.config)
        config['chat_entitlements']['bob']=['chat-b']
        with patch.object(socket,'socket',side_effect=AssertionError('network forbidden')), \
                patch.object(urllib.request.HTTPHandler,'http_open',fake_http_open):
            app=create_readonly_beeper_app(config,beeper_token_provider=lambda caller:'backend-'+caller.subject)
            # Mutating the source config must not broaden the startup snapshot.
            config['chat_entitlements']['alice'].append('chat-b')
            def invoke(subject,chat):
                body=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call',
                    'params':{'name':'list_messages','arguments':{'chatID':chat}}}).encode()
                statuses=[]
                data=app({'REQUEST_METHOD':'POST','CONTENT_TYPE':'application/json',
                    'CONTENT_LENGTH':str(len(body)),'wsgi.input':io.BytesIO(body),
                    'HTTP_AUTHORIZATION':'Bearer '+token_for(subject)},
                    lambda status,headers:statuses.append(status))
                return statuses[0],json.loads(b''.join(data))
            for subject,chat in [('alice','chat-a'),('bob','chat-b'),('alice','chat-a')]:
                status,result=invoke(subject,chat)
                self.assertTrue(status.startswith('200'))
                content=json.loads(result['result']['content'][0]['text'])
                self.assertEqual(content['chat_id'],chat)
                self.assertEqual(content['messages'],[{'id':'synthetic','text':'synthetic body'}])
                self.assertNotIn('unprojected synthetic metadata',json.dumps(content))
                self.assertEqual(observed[-5:], [('Bearer backend-'+subject,method) for method in
                    ('initialize','notifications/initialized','tools/list','resolve_chat_identity','tools/call')])
            count=len(observed)
            status,result=invoke('alice','chat-b')
            self.assertTrue(status.startswith('403'))
            self.assertEqual(len(observed),count)

    def test_missing_fields(self):
        for field in self.config:
            config=copy.deepcopy(self.config); del config[field]
            with self.subTest(field=field), self.assertRaises(Exception): self.build(config)

    def test_bad_boundary_values(self):
        for field,value in [('issuer','http://issuer.example.invalid'),('resource','https://user:password@example.invalid/mcp'),
            ('beeper_url','https://remote.example.invalid/mcp'),('beeper_url','http://localhost/mcp?token=no'),
            ('chat_entitlements',{'alice':'chat-a'}),('jwks',{'keys':[]})]:
            config=copy.deepcopy(self.config);config[field]=value
            with self.subTest(field=field,value=value), self.assertRaises(Exception):self.build(config)

    def test_reject_private_key_and_wrong_algorithm(self):
        for field,value in [('d','private'),('alg','HS256'),('use','enc'),('key_ops',['sign'])]:
            config=copy.deepcopy(self.config);config['jwks']['keys'][0][field]=value
            with self.subTest(field=field), self.assertRaises(Exception):self.build(config)

    def test_reject_auth_disable_and_missing_provider(self):
        config=copy.deepcopy(self.config);config['auth_required']=False
        with self.assertRaises(Exception):self.build(config)
        with self.assertRaises(Exception):create_readonly_beeper_app(self.config,beeper_token_provider=None)

if __name__ == '__main__': unittest.main()
