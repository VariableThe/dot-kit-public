"""OAuth descriptor and transport-level reauthorization regression tests."""
import unittest
from unittest.mock import Mock, patch

import test_beeper_http as helpers
from plugins.ecosystem_connectivity.beeper_mcp_plugin import BeeperMCPPlugin


class ToolAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        helpers.HTTPTests.setUpClass()

    def setUp(self):
        self.h = helpers.HTTPTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.provider = Mock(side_effect=AssertionError('unauthorized backend access'))
        self.h.app = self.h.build(provider=self.provider)
        self.call = {'jsonrpc':'2.0','id':7,'method':'tools/call',
                     'params':{'name':'list_messages','arguments':{'chatID':'a'}}}

    def test_descriptors_match_and_are_independent(self):
        tools = BeeperMCPPlugin._tools()
        self.assertEqual({t['name'] for t in tools}, {'search_chats','list_messages'})
        for tool in tools:
            expected=[{'type':'oauth2','scopes':['beeper:read']}]
            self.assertEqual(tool['securitySchemes'],expected)
            self.assertEqual(tool['_meta']['securitySchemes'],expected)
            self.assertEqual(tool['annotations'],{'readOnlyHint':True,'destructiveHint':False,
                'openWorldHint':False,'idempotentHint':True})
        tools[0]['securitySchemes'][0]['scopes'].append('write')
        self.assertEqual(tools[0]['_meta']['securitySchemes'][0]['scopes'],['beeper:read'])
        self.assertEqual(BeeperMCPPlugin._tools()[0]['securitySchemes'][0]['scopes'],['beeper:read'])
        status,_,body=self.h.call(token=self.h.token())
        self.assertEqual(status,200)
        self.assertEqual(body['result']['tools'],BeeperMCPPlugin._tools())
        self.provider.assert_not_called()

    def test_missing_expired_wrong_audience_never_reach_backend(self):
        for token in (None,self.h.token(exp=1),self.h.token(aud='https://other.invalid')):
            with self.subTest(token_state='missing' if token is None else 'invalid'):
                status,headers,body=self.h.call(self.call,token)
                self.assertEqual(status,401)
                self.assertIn('resource_metadata=',headers['WWW-Authenticate'])
                self.assertNotIn('result',body)
                self.provider.assert_not_called()

    def test_scope_denial_stays_http_forbidden(self):
        for scope in ('','beeper:write'):
            status,headers,body=self.h.call(self.call,self.h.token(scope=scope))
            self.assertEqual(status,403)
            self.assertIn('error="insufficient_scope"',headers['WWW-Authenticate'])
            self.assertIn('scope="beeper:read"',headers['WWW-Authenticate'])
            self.assertNotIn('result',body)
        self.provider.assert_not_called()

    def test_entitlement_denial_is_not_reauthentication(self):
        status,headers,body=self.h.call(self.call,self.h.token(sub='bob'))
        self.assertEqual(status,403)
        self.assertNotIn('WWW-Authenticate',headers)
        self.assertNotIn('mcp/www_authenticate',str(body))
        self.provider.assert_not_called()

    def test_fresh_authorization_after_expiry_rechecks_scope_and_chat(self):
        self.assertEqual(self.h.call(self.call,self.h.token(exp=1))[0],401)
        self.assertEqual(self.h.call(self.call,self.h.token(scope=''))[0],403)
        self.provider.assert_not_called()
        # Fresh valid authentication must not bypass the real tool's chat gate.
        wrong_chat = {'jsonrpc':'2.0','id':8,'method':'tools/call',
                      'params':{'name':'list_messages','arguments':{'chatID':'b'}}}
        status,headers,body=self.h.call(wrong_chat,self.h.token())
        self.assertEqual(status,403)
        self.assertNotIn('WWW-Authenticate',headers)
        self.provider.assert_not_called()
        # Patch only execution after authorization, preserving actual signed
        # JWT verification and scope checks. No synthetic token reaches a socket.
        with patch.object(BeeperMCPPlugin,'_call_read',return_value={'content':[]}) as read:
            self.assertEqual(self.h.call(self.call,self.h.token())[0],200)
            read.assert_called_once()
            caller=read.call_args.args[0]
            self.assertEqual(caller.subject,'alice')
            self.assertEqual(caller.allowed_chats,frozenset({'a'}))
        self.provider.assert_not_called()

if __name__=='__main__':unittest.main()
