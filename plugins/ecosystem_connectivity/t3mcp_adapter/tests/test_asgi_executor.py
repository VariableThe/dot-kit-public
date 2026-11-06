"""Deterministic, socket-free ASGI executor lifecycle tests."""
import asyncio
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from t3mcp.mcp_http import HTTPResponse, MCPResourceServer, create_asgi_app


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        return SimpleNamespace(clock=time.monotonic, policy=SimpleNamespace(
            auth_required=True, auth={'issuer': 'https://as.test',
                                      'audience': 'https://adapter.test/mcp'},
            limits={'request_budget_s': 10, 'call_timeout_s': 10}))

    def app(self, ctx=None, **kwargs):
        return create_asgi_app(ctx or self.context(),
                               resource='https://adapter.test/mcp', **kwargs)

    async def call(self, app, token='one', receive=None):
        messages = []
        async def default_receive():
            return {'type': 'http.request', 'body': b'{}'}
        async def send(message):
            messages.append(message)
        await app({'type': 'http', 'method': 'POST', 'path': '/mcp',
                   'headers': [(b'authorization', token.encode())]},
                  receive or default_receive, send)
        return messages

    def blocked_handler(self, loop, entered, release, records):
        def handle(server, method, path, headers, body, *, deadline):
            records.append((dict(headers)['authorization'], deadline, threading.get_ident()))
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise AssertionError('test did not release worker')
            return HTTPResponse(200, {}, records[-1][0].encode())
        return handle

    async def test_heartbeat_overload_and_no_header_crossover(self):
        entered, release, records = asyncio.Event(), threading.Event(), []
        app = self.app()
        with patch.object(MCPResourceServer, 'handle', self.blocked_handler(
                asyncio.get_running_loop(), entered, release, records)):
            first = asyncio.create_task(self.call(app))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                heartbeat = asyncio.Event()
                asyncio.get_running_loop().call_soon(heartbeat.set)
                await asyncio.wait_for(heartbeat.wait(), 1)
                second = await self.call(app, 'two')
                self.assertEqual(second[0]['status'], 503)
                self.assertEqual(len(records), 1)
                self.assertNotEqual(records[0][2], threading.get_ident())
            finally:
                release.set()
            self.assertEqual((await first)[1]['body'], b'one')
            self.assertEqual((await self.call(app, 'two'))[1]['body'], b'two')
            self.assertEqual([r[0] for r in records], ['one', 'two'])
            self.assertTrue(await app.aclose())

    async def test_worker_completion_after_thread_event_is_observed(self):
        entered, release, records = asyncio.Event(), threading.Event(), []
        app = self.app()
        with patch.object(MCPResourceServer, 'handle', self.blocked_handler(
                asyncio.get_running_loop(), entered, release, records)):
            first = asyncio.create_task(self.call(app))
            await asyncio.wait_for(entered.wait(), 1)
            release.set()
            # The adapter budget is ten seconds; keep this bound above it so
            # slow sandbox thread scheduling cannot make the regression flaky.
            messages = await asyncio.wait_for(first, 12)
            self.assertEqual(messages[0]['status'], 200)
            self.assertEqual(messages[1]['body'], b'one')
            self.assertTrue(await app.aclose())

    async def test_worker_exception_returns_generic_error_and_releases_slot(self):
        app = self.app()
        calls = []
        def failing_then_healthy(server, method, path, headers, body, *, deadline):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('private backend detail')
            return HTTPResponse(200, {}, b'recovered')
        with patch.object(MCPResourceServer, 'handle', failing_then_healthy):
            first = await self.call(app)
            self.assertEqual(first[0]['status'], 503)
            self.assertEqual(first[1]['body'], b'{"error":"backend_unavailable"}')
            self.assertNotIn(b'private backend detail', first[1]['body'])
            second = await self.call(app)
            self.assertEqual(second[0]['status'], 200)
            self.assertEqual(second[1]['body'], b'recovered')
            self.assertEqual(len(calls), 2)
            self.assertTrue(await app.aclose())

    async def test_cancel_keeps_capacity_until_worker_finishes(self):
        entered, release, records = asyncio.Event(), threading.Event(), []
        app = self.app()
        with patch.object(MCPResourceServer, 'handle', self.blocked_handler(
                asyncio.get_running_loop(), entered, release, records)):
            first = asyncio.create_task(self.call(app))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assertEqual((await self.call(app))[0]['status'], 503)
                self.assertEqual(len(records), 1)
            finally:
                release.set()
            self.assertTrue(await app.aclose())

    async def test_shutdown_is_bounded_and_stops_admission(self):
        entered, release, records = asyncio.Event(), threading.Event(), []
        app = self.app(shutdown_grace_s=0)
        with patch.object(MCPResourceServer, 'handle', self.blocked_handler(
                asyncio.get_running_loop(), entered, release, records)):
            first = asyncio.create_task(self.call(app))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertFalse(await asyncio.wait_for(app.aclose(), 1))
                self.assertFalse(first.done())
                self.assertEqual((await self.call(app))[0]['status'], 503)
            finally:
                release.set()
            await first
            self.assertTrue(await app.aclose())
            self.assertEqual((await self.call(app))[0]['status'], 503)

    async def test_deadline_is_fixed_before_ingress(self):
        ctx = self.context()
        clock = [100.0]
        ctx.clock = lambda: clock[0]
        app = self.app(ctx)
        seen = []
        def handle(server, method, path, headers, body, *, deadline):
            seen.append(deadline)
            return HTTPResponse(200, {}, b'')
        async def receive():
            clock[0] = 107.0
            return {'type': 'http.request', 'body': b'{}'}
        with patch.object(MCPResourceServer, 'handle', handle):
            await self.call(app, receive=receive)
        self.assertEqual(seen, [110.0])
        await app.aclose()

    async def test_timeout_keeps_capacity(self):
        ctx = self.context()
        ctx.policy.limits['request_budget_s'] = .05
        entered, release, records = asyncio.Event(), threading.Event(), []
        app = self.app(ctx)
        with patch.object(MCPResourceServer, 'handle', self.blocked_handler(
                asyncio.get_running_loop(), entered, release, records)):
            first = asyncio.create_task(self.call(app))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertEqual((await first)[0]['status'], 504)
                self.assertEqual((await self.call(app))[0]['status'], 503)
            finally:
                release.set()
            await app.aclose()

    async def test_lifespan_shutdown_drains(self):
        app = self.app()
        incoming = iter([{'type': 'lifespan.startup'}, {'type': 'lifespan.shutdown'}])
        outgoing = []
        async def receive():
            return next(incoming)
        async def send(message):
            outgoing.append(message['type'])
        await app({'type': 'lifespan'}, receive, send)
        self.assertEqual(outgoing, ['lifespan.startup.complete', 'lifespan.shutdown.complete'])
        self.assertEqual((await self.call(app))[0]['status'], 503)

    async def test_graceful_shutdown_waits_for_actual_completion(self):
        entered, release, records = asyncio.Event(), threading.Event(), []
        app = self.app()
        with patch.object(MCPResourceServer, 'handle', self.blocked_handler(
                asyncio.get_running_loop(), entered, release, records)):
            first = asyncio.create_task(self.call(app))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                closing = asyncio.create_task(app.aclose())
                # This marker runs after close begins its await, without sleeps.
                marker = asyncio.Event()
                asyncio.get_running_loop().call_soon(marker.set)
                await marker.wait()
                self.assertFalse(closing.done())
                self.assertEqual((await self.call(app))[0]['status'], 503)
            finally:
                release.set()
            await first
            self.assertTrue(await closing)

    async def test_body_timeout_never_submits_handler(self):
        ctx = self.context()
        ctx.policy.limits['call_timeout_s'] = .01
        app = self.app(ctx)
        body_wait = asyncio.Event()
        async def receive():
            await body_wait.wait()
        with patch.object(MCPResourceServer, 'handle') as handler:
            result = await self.call(app, receive=receive)
            self.assertEqual(result[0]['status'], 408)
            handler.assert_not_called()
        await app.aclose()

    async def test_lifespan_reports_incomplete_drain(self):
        entered, release, records = asyncio.Event(), threading.Event(), []
        app = self.app(shutdown_grace_s=0)
        with patch.object(MCPResourceServer, 'handle', self.blocked_handler(
                asyncio.get_running_loop(), entered, release, records)):
            first = asyncio.create_task(self.call(app))
            outgoing = []
            async def receive():
                return {'type': 'lifespan.shutdown'}
            async def send(message):
                outgoing.append(message)
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await app({'type': 'lifespan'}, receive, send)
                self.assertEqual(outgoing[0]['type'], 'lifespan.shutdown.failed')
                self.assertFalse(first.done())
            finally:
                release.set()
            await first
