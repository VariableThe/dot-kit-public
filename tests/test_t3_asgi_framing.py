"""Isolated real ASGI ingress tests; tool dispatch/imports are synthetic."""
import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import sys
import time
import types
import unittest
from unittest.mock import patch


ROOT=Path(__file__).resolve().parents[1]/'plugins/ecosystem_connectivity/t3mcp_adapter/t3mcp'

def load_boundary():
    name='_isolated_t3_framing'
    package=types.ModuleType(name);package.__path__=[str(ROOT)]
    tools=types.ModuleType(name+'.tools');tools.dispatch=lambda *a: {};tools.tool_specs=lambda *a:[]
    errors=types.ModuleType(name+'.errors');errors.AdapterError=type('AdapterError',(Exception,),{})
    spec=importlib.util.spec_from_file_location(name+'.mcp_http',ROOT/'mcp_http.py')
    module=importlib.util.module_from_spec(spec)
    # Keep stubs confined to this load; imported objects survive without
    # contaminating repository modules or other test files.
    with patch.dict(sys.modules,{name:package,name+'.tools':tools,name+'.errors':errors,
                                 name+'.mcp_http':module}):spec.loader.exec_module(module)
    return module


m=load_boundary()

class FramingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.socket_patch=patch.object(socket,'socket',side_effect=AssertionError('network forbidden'))
        self.socket_patch.start();self.addCleanup(self.socket_patch.stop)
        self.ctx=types.SimpleNamespace(policy=types.SimpleNamespace(auth_required=True,
            auth={'issuer':'https://issuer.invalid','audience':'https://mcp.invalid/mcp'},
            limits={'request_budget_s':2,'call_timeout_s':1}),clock=time.monotonic)

    async def invoke(self,headers,chunks=(b'{}',),**limits):
        app=m.create_asgi_app(self.ctx,resource='https://mcp.invalid/mcp',**limits)
        output=[];reads=[];dispatch=[]
        async def receive():
            index=len(reads);reads.append(index)
            return {'type':'http.request','body':chunks[index],'more_body':index<len(chunks)-1}
        async def send(value):output.append(value)
        def handle(*args,**kwargs):dispatch.append((args,kwargs));return m.HTTPResponse(200,{},b'{}')
        try:
            with patch.object(m.MCPResourceServer,'handle',handle):
                await app({'type':'http','method':'POST','path':'/mcp','headers':headers},receive,send)
        finally:await app.aclose()
        return output[0]['status'],reads,dispatch

    async def test_duplicate_and_smuggling_headers_rejected_before_body(self):
        cases=[[(b'authorization',b'a'),(b'Authorization',b'b')],
               [(b'content-length',b'2'),(b'Content-Length',b'2')],
               [(b'transfer-encoding',b'chunked')],
               [(b'content-length',b'2'),(b'transfer-encoding',b'chunked')],
               [(b'content-length',b'2,2')],[(b'content-length',b' 2')],
               [(b'content-length',b'+2')],[(b'content-length',b'')]]
        for headers in cases:
            with self.subTest(headers=headers):
                status,reads,dispatch=await self.invoke(headers)
                self.assertEqual(status,400);self.assertFalse(reads);self.assertFalse(dispatch)

    async def test_malformed_raw_headers_fail_closed(self):
        for headers in [[(b'x',b'a\r\nb')],[(b'x',b'a\x00b')],[(b'x',b'\tbad')],
                        [(b'bad name',b'x')],[(b'content_length',b'2')],
                        [('host','x')],[(b'x',)],[(b'x',b'y',b'z')]]:
            status,reads,dispatch=await self.invoke(headers)
            self.assertEqual(status,400);self.assertFalse(reads);self.assertFalse(dispatch)

    async def test_intentional_narrow_hosting_contract(self):
        # These can be valid ASGI/HTTP inputs, but are outside this adapter's
        # documented deployment contract. Reject before reading or dispatching.
        cases=[(iter([(b'host',b'mcp.invalid')]),431),
               ([(b'cookie',b'a=1'),(b'cookie',b'b=2')],400),
               ([(b'transfer-encoding',b'chunked')],400),  # body already dechunked
               ([(b'x_custom',b'value')],400),
               ([(b'x-custom',b'a\tb')],400)]
        for headers,expected in cases:
            status,reads,dispatch=await self.invoke(headers,chunks=(b'{}',))
            self.assertEqual(status,expected);self.assertFalse(reads);self.assertFalse(dispatch)
        status,_,dispatch=await self.invoke(((b'cookie',b'a=1; b=2'),))
        self.assertEqual(status,200);self.assertEqual(len(dispatch),1)

    async def test_header_and_declared_size_bounds(self):
        for headers,limits,status in [([(b'x',b'abcdef')],{'max_header_bytes':4},431),
            ([(b'content-length',b'3')],{'max_body_bytes':2},413),
            ([(str(i).encode(),b'x') for i in range(129)],{},431)]:
            actual,reads,dispatch=await self.invoke(headers,**limits)
            self.assertEqual(actual,status);self.assertFalse(reads);self.assertFalse(dispatch)

    async def test_short_long_and_chunked_mismatch_never_dispatch(self):
        for length,chunks in [(b'3',(b'{}',)),(b'1',(b'{}',)),(b'2',(b'{',b'}x')),(b'0',(b'x',))]:
            status,_,dispatch=await self.invoke([(b'content-length',length)],chunks)
            self.assertEqual(status,400);self.assertFalse(dispatch)

    async def test_exact_length_and_optional_length_dispatch(self):
        for headers,chunks in [([(b'content-length',b'2')],(b'{',b'}')),([], (b'{}',)),
                               ([(b'content-length',b'0')],(b'',))]:
            status,_,dispatch=await self.invoke(headers,chunks)
            self.assertEqual(status,200);self.assertEqual(len(dispatch),1)

    async def test_invalid_configured_bounds(self):
        for value in (True,False,0,-1,1.5,float('inf')):
            for name in ('max_body_bytes','max_header_bytes'):
                with self.assertRaises(ValueError):m.create_asgi_app(self.ctx,resource='https://mcp.invalid/mcp',**{name:value})

if __name__=='__main__':unittest.main()
