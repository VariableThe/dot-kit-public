import base64
import copy
import importlib.util
import io
import json
import socket
import unittest
from unittest.mock import patch
from plugins.ecosystem_connectivity import preflight as p


def integer(n):
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length()+7)//8,'big')).rstrip(b'=').decode()


def plan(plugin='beeper'):
    # Shape-only synthetic public modulus; no signing credential is created.
    config={'resource':'https://mcp.example.invalid/'+('mcp' if plugin=='t3' else 'beeper'),
      'issuer':'https://issuer.example.invalid',
      'jwks':{'keys':[{'kty':'RSA','kid':'synthetic','n':integer((1<<2047)+1),'e':integer(65537)}]}}
    if plugin=='beeper':config.update(beeper_url='http://127.0.0.1:23373/v0/mcp',chat_entitlements={'alice':['chat-a']})
    else:config.update(hosts={'local':{'url':'http://127.0.0.1:3773/mcp','credential_ref':'secret-store/t3'}},
                       allowed_projects=['11111111-1111-4111-8111-111111111111'])
    return {'plugin':plugin,'config':config,
      'authorization':{'required':True,'algorithm':'RS256','issuer':config['issuer'],'audience':config['resource'],
        'scopes':[plugin+':read'],'server_metadata':True,'pkce_s256':True,'client_registration':True,'subject_authorization':True},
      'hosting':dict.fromkeys(p.GATEWAY_FIELDS,True),
      'credential_provider':dict.fromkeys(('configured','independent_from_caller','account_bound'),True)}


class PreflightTests(unittest.TestCase):
    def reject(self,document):
        with self.assertRaises(p.ConfigError):p.validate(document)

    def test_both_plugins_without_io(self):
        with patch('builtins.open',side_effect=AssertionError('file read')),patch.object(socket,'socket',side_effect=AssertionError('network')):
            for plugin in ('beeper','t3'):
                result=p.validate(plan(plugin))
                self.assertTrue(result['configuration_valid']);self.assertFalse(result['live_readiness_verified'])
                self.assertFalse(result['credentials_read']);self.assertFalse(result['listener_started'])
                self.assertNotIn('alice',json.dumps(result))

    def test_import_does_not_read_config_credentials_or_start_network(self):
        spec=importlib.util.spec_from_file_location('isolated_preflight',p.__file__)
        module=importlib.util.module_from_spec(spec)
        with patch('builtins.open',side_effect=AssertionError('read')),patch.object(socket,'socket',side_effect=AssertionError('network')):
            spec.loader.exec_module(module)

    def test_exact_authorization(self):
        for key,value in [('required',False),('algorithm','HS256'),('audience','https://other.invalid'),
                          ('issuer','https://other.invalid'),('scopes',['beeper:write']),('pkce_s256',1)]:
            doc=plan();doc['authorization'][key]=value
            with self.subTest(key=key):self.reject(doc)

    def test_gateway_and_provider_required(self):
        for section in ('hosting','credential_provider'):
            for key in plan()[section]:
                doc=plan();doc[section][key]=False;self.reject(doc)

    def test_public_keys(self):
        for key,value in [('d','secret'),('kty','oct'),('alg','HS256'),('n',integer(17)),('e',integer(2)),('n','bad===')]:
            doc=plan();doc['config']['jwks']['keys'][0][key]=value
            with self.subTest(key=key,value=value):self.reject(doc)
        doc=plan();doc['config']['jwks']['keys']*=2;self.reject(doc)

    def test_urls_and_inline_secrets(self):
        for value in ('http://mcp.example.invalid','https://user:secret@mcp.example.invalid','https://mcp.example.invalid/?token=x','https://mcp.example.invalid:0/mcp'):
            doc=plan();doc['config']['resource']=value;self.reject(doc)
        doc=plan();doc['config']['beeper_url']='https://remote.example.invalid/mcp';self.reject(doc)
        doc=plan();doc['credential_provider']['token']='secret';self.reject(doc)

    def test_entitlements(self):
        for value in ({},{'*':['chat']},{'alice':[]},{'alice':['*']},{'alice':['a','a']},{'alice':'chat'}):
            doc=plan();doc['config']['chat_entitlements']=value;self.reject(doc)

    def test_t3_hosts_projects_and_write_rejection(self):
        doc=plan('t3');doc['config']['allowed_projects']=['title'];self.reject(doc)
        doc=plan('t3');doc['config']['hosts']['local']['url']='http://external.invalid/mcp';self.reject(doc)
        doc=plan('t3');doc['config']['allow_writes']=True;self.reject(doc)
        doc=plan('t3');doc['config']['hosts']['local']['token']='secret';self.reject(doc)
        self.assertIn('shared',p.validate(plan('t3'))['warnings'][0])

    def cli(self,raw):
        output=io.StringIO()
        with patch('builtins.open',return_value=io.BytesIO(raw)) as opened,patch('sys.stdout',output):
            status=p.main(['--config','explicit-plan.json'])
        opened.assert_called_once_with('explicit-plan.json','rb')
        return status,output.getvalue()

    def test_malformed_arguments_do_not_echo_values(self):
        for arguments in (['--unknown-secret-value'], ['--config'],
                          ['--config', 'secret-path', 'secret-extra'],
                          ['--serve', 'secret-token']):
            stderr,stdout=io.StringIO(),io.StringIO()
            with patch('sys.stderr',stderr),patch('sys.stdout',stdout), \
                    patch('builtins.open',side_effect=AssertionError('config read')):
                with self.assertRaises(SystemExit) as result:p.main(arguments)
            self.assertEqual(result.exception.code,2)
            self.assertEqual(stderr.getvalue(),'invalid command-line arguments\n')
            self.assertEqual(stdout.getvalue(),'')

    def test_cli_no_listener_and_redacted_failures(self):
        status,out=self.cli(json.dumps(plan()).encode());self.assertEqual(status,0)
        self.assertFalse(json.loads(out)['live_readiness_verified'])
        for raw in (b'{"secret":"do-not-print",',b'{"x":1,"x":2}',b'{"x":1e9999}',b'[]',b'x'*(p.MAX_CONFIG_BYTES+1)):
            status,out=self.cli(raw);self.assertEqual(status,2);self.assertNotIn('do-not-print',out)
            self.assertEqual(json.loads(out)['error'],'configuration rejected')

if __name__=='__main__':unittest.main()
