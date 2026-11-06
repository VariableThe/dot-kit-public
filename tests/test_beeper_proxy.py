import io
import json
import os
import socket
import unittest
import urllib.request
import urllib.response
from unittest.mock import patch
from plugins.ecosystem_connectivity.beeper_mcp_readonly import StreamableHTTPTransport


class ProxyTests(unittest.TestCase):
    def test_ambient_proxy_cannot_receive_loopback_bearer(self):
        observed=[]
        def fake_http_open(handler, request):
            observed.append((request.host,request.full_url,request.get_header('Authorization')))
            response=urllib.response.addinfourl(io.BytesIO(b'{"jsonrpc":"2.0","id":1,"result":{}}'),{},request.full_url,200)
            response.msg='OK'
            return response
        # Deliberately omit no_proxy. Exercise urllib's actual handler chain,
        # replacing only its final HTTP network operation.
        with patch.dict(os.environ,{'http_proxy':'http://proxy.example.invalid:9000',
                'https_proxy':'http://proxy.example.invalid:9000'},clear=True), \
                patch.object(socket,'socket',side_effect=AssertionError('network forbidden')), \
                patch.object(urllib.request.HTTPHandler,'http_open',fake_http_open):
            transport=StreamableHTTPTransport('http://127.0.0.1:12345/mcp')
            self.assertFalse(any(isinstance(h,urllib.request.ProxyHandler) and h.proxies
                                 for h in transport.opener.handlers))
            transport.post({'jsonrpc':'2.0','id':1,'method':'tools/list'},'synthetic-backend-token')
            transport.notify({'jsonrpc':'2.0','method':'notifications/initialized'},'synthetic-backend-token')
        self.assertEqual(observed,[('127.0.0.1:12345','http://127.0.0.1:12345/mcp',
                                    'Bearer synthetic-backend-token')]*2)

if __name__ == '__main__': unittest.main()
