"""Small HTTP resource-server boundary for the private MCP adapter.

This module intentionally exposes a framework-neutral request handler so the
security boundary can be tested without binding a port or contacting an
authorization server. The handler is the only supported bridge from HTTP to
``t3mcp.tools.dispatch``: bearer credentials come from the HTTP
``Authorization`` header, never from model-controlled JSON-RPC parameters.

The authorization server is external and operator-selected. This adapter does
not mint tokens, register clients, or implement an OAuth token endpoint.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import AdapterError
from .tools import dispatch, tool_specs


def _reject_json_constant(value):
    raise ValueError("non-standard JSON constant")


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _header_items(headers):
    if isinstance(headers, dict):
        return list(headers.items())
    if isinstance(headers, (list, tuple)):
        return list(headers)
    return []


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: dict
    body: bytes


def _json_response(status, value, headers=None):
    merged = {"Content-Type": "application/json", "Cache-Control": "no-store"}
    if headers:
        merged.update(headers)
    return HTTPResponse(status, merged, json.dumps(value, separators=(",", ":")).encode())


def _bearer(headers):
    value = None
    for key, candidate in (headers or {}).items():
        if key.lower() == "authorization":
            value = candidate
            break
    if not isinstance(value, str):
        return None
    scheme, separator, credential = value.strip().partition(" ")
    if not separator or scheme.lower() != "bearer" or not credential.strip():
        return None
    credential = credential.strip()
    return credential if len(credential) <= 4096 else None


def _asgi_headers(raw_headers, max_header_bytes, max_body_bytes):
    """Validate raw ASGI byte pairs before decoding/coalescing anything.

    ASGI servers must already reject ambiguous wire framing. This is a second
    boundary, not a replacement for that server requirement. Duplicate fields
    (including otherwise repeatable ones) and transfer encoding are unsupported.
    """
    if not isinstance(raw_headers, (list, tuple)) or len(raw_headers) > 128:
        return None, None, _json_response(431, {"error": "headers_too_large"})
    headers, seen, total, length = [], set(), 0, None
    for pair in raw_headers:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None, None, _json_response(400, {"error": "invalid_header"})
        key, value = pair
        if not isinstance(key, bytes) or not isinstance(value, bytes):
            return None, None, _json_response(400, {"error": "invalid_header"})
        total += len(key) + len(value)
        if total > max_header_bytes:
            return None, None, _json_response(431, {"error": "headers_too_large"})
        if not re.fullmatch(rb"[!#$%&'*+.^`|~0-9A-Za-z-]+", key) or any(
                byte < 32 or byte == 127 for byte in value):
            return None, None, _json_response(400, {"error": "invalid_header"})
        name = key.lower()
        if name in seen:
            return None, None, _json_response(400, {"error": "duplicate_header"})
        seen.add(name)
        if name == b"transfer-encoding":
            return None, None, _json_response(400, {"error": "transfer_encoding_unsupported"})
        if name == b"content-length":
            if not re.fullmatch(rb"[0-9]{1,20}", value):
                return None, None, _json_response(400, {"error": "invalid_content_length"})
            length = int(value)
            if length > max_body_bytes:
                return None, None, _json_response(413, {"error": "request_too_large"})
        headers.append((key.decode("ascii"), value.decode("latin-1")))
    return headers, length, None


class MCPResourceServer:
    """Framework-neutral MCP resource server for one configured ToolContext."""

    def __init__(self, ctx, *, resource=None, authorization_server=None,
                 metadata_path="/.well-known/oauth-protected-resource",
                 mcp_path="/mcp", allowed_origins=(), max_body_bytes=1_000_000,
                 max_header_bytes=16_384):
        for limit in (max_body_bytes, max_header_bytes):
            if type(limit) is not int or limit <= 0:
                raise ValueError("HTTP byte limits must be positive integers")
        self.ctx = ctx
        self.mcp_path = mcp_path
        self.metadata_path = metadata_path
        self.allowed_origins = frozenset(allowed_origins)
        self.max_body_bytes = max_body_bytes
        self.max_header_bytes = max_header_bytes
        self.resource = resource
        auth = getattr(ctx.policy, "auth", None) or {}
        if not getattr(ctx.policy, "auth_required", False):
            raise ValueError("HTTP resource server requires auth.required=true")
        self.authorization_server = authorization_server or auth.get("issuer")
        resource_parts = urlsplit(self.resource) if isinstance(self.resource, str) else None
        try:
            resource_parts.port if resource_parts else None
        except ValueError as exc:
            raise ValueError("resource must have a valid port") from exc
        if (resource_parts is None or resource_parts.scheme not in ("http", "https") or
                not resource_parts.netloc or resource_parts.username or resource_parts.password or
                resource_parts.fragment or (resource_parts.scheme != "https" and
                resource_parts.hostname not in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("resource must be an explicit absolute HTTP(S) URL")
        as_parts = urlsplit(self.authorization_server) if isinstance(self.authorization_server, str) else None
        try:
            as_parts.port if as_parts else None
        except ValueError as exc:
            raise ValueError("authorization_server must have a valid port") from exc
        if (as_parts is None or as_parts.scheme not in ("http", "https") or not as_parts.netloc or
                as_parts.username or as_parts.password or as_parts.fragment or
                (as_parts.scheme != "https" and as_parts.hostname not in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("authorization_server must be an absolute HTTP(S) URL")
        if auth and auth.get("audience") != self.resource:
            raise ValueError("resource must exactly match the JWT audience")
        if auth and auth.get("issuer") != self.authorization_server:
            raise ValueError("authorization_server must exactly match the JWT issuer")

    def protected_resource_metadata(self):
        return {
            "resource": self.resource,
            "authorization_servers": [self.authorization_server],
            "scopes_supported": ["t3:read", "t3:write"],
            "bearer_methods_supported": ["header"],
        }

    def _challenge(self, *, invalid=False):
        value = 'Bearer resource_metadata="' + self.resource_metadata_url + '"'
        if invalid:
            value += ', error="invalid_token"'
        return {"WWW-Authenticate": value}

    @property
    def resource_metadata_url(self):
        parts = urlsplit(self.resource)
        return f"{parts.scheme}://{parts.netloc}{self.metadata_path}"

    def handle(self, method, path, headers=None, body=b"", *, deadline=None):
        header_items = _header_items(headers)
        lowered = {}
        for key, value in header_items:
            name = str(key).lower()
            if name in lowered:
                return _json_response(400, {"error": "duplicate_header"})
            lowered[name] = value
        origin = lowered.get("origin")
        if origin is not None and origin not in self.allowed_origins:
            return _json_response(403, {"error": "origin_denied"})
        if method == "GET" and path == self.metadata_path:
            return _json_response(200, self.protected_resource_metadata(),
                                  {"Access-Control-Allow-Origin": "*"})
        if path == self.mcp_path and method == "GET":
            return _json_response(405, {"error": "method_not_allowed"}, {"Allow": "POST"})
        if method != "POST" or path != self.mcp_path:
            return _json_response(404, {"error": "not_found"})

        if isinstance(body, str):
            body = body.encode("utf-8")
        if not isinstance(body, (bytes, bytearray)) or len(body) > self.max_body_bytes:
            return _json_response(413, {"error": "request_too_large"})
        header_bytes = sum(len(str(key).encode("utf-8")) + len(str(value).encode("utf-8"))
                           for key, value in header_items)
        if header_bytes > self.max_header_bytes:
            return _json_response(431, {"error": "headers_too_large"})
        version_header = lowered.get("mcp-protocol-version")
        if version_header is not None and version_header not in {"2024-11-05", "2025-06-18"}:
            return _json_response(400, {"error": "unsupported_protocol_version"})

        token = _bearer(dict(header_items))
        if token is None:
            return _json_response(401, {"error": "unauthorized"}, self._challenge())
        # Authenticate every HTTP JSON-RPC request, including initialize and
        # tools/list. A non-empty bearer string is never sufficient.
        try:
            self.ctx.authenticate(token)
        except AdapterError:
            return _json_response(401, {"error": "invalid_token"}, self._challenge(invalid=True))
        try:
            payload = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body,
                                parse_constant=_reject_json_constant,
                                object_pairs_hook=_reject_duplicate_pairs)
        except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
            return _json_response(400, {"error": "invalid_json"})
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            return _json_response(400, {"error": "invalid_jsonrpc"})
        request_id = payload.get("id")
        if request_id is not None and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
            return self._rpc_error(None, -32600, "invalid request id")
        method_name = payload.get("method")
        params = payload.get("params", {})
        if not isinstance(method_name, str) or not isinstance(params, dict):
            return self._rpc_error(request_id, -32600, "invalid request")
        if method_name in {"tools/call", "tools/list", "initialize", "ping"} and request_id is None:
            return self._rpc_error(request_id, -32600, "request id is required")
        if method_name == "notifications/initialized":
            if request_id is not None:
                return self._rpc_error(None, -32600, "notification must not include an id")
            return HTTPResponse(202, {}, b"")
        if method_name == "initialize":
            version = params.get("protocolVersion")
            client_info = params.get("clientInfo")
            if not isinstance(version, str) or not isinstance(params.get("capabilities"), dict) or not isinstance(client_info, dict) or not isinstance(client_info.get("name"), str) or not client_info["name"] or not isinstance(client_info.get("version"), str) or not client_info["version"]:
                return self._rpc_error(request_id, -32602, "unsupported protocolVersion")
            if version not in {"2024-11-05", "2025-06-18"}:
                version = "2025-06-18"
            return _json_response(200, {"jsonrpc": "2.0", "id": request_id,
                                        "result": {"protocolVersion": version,
                                                    "capabilities": {"tools": {"listChanged": False}},
                                                    "serverInfo": {"name": "t3mcp", "version": "0.1.0"}}})
        if method_name == "ping":
            return _json_response(200, {"jsonrpc": "2.0", "id": request_id, "result": {}})
        if method_name == "tools/list":
            return _json_response(200, {"jsonrpc": "2.0", "id": request_id,
                                        "result": {"tools": self._tool_specs()}})
        if method_name != "tools/call":
            return self._rpc_error(request_id, -32601, "method not found")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return self._rpc_error(request_id, -32602, "invalid tools/call parameters")
        # Deliberately ignore/reject model-supplied auth metadata. A caller may
        # not smuggle an identity through JSON-RPC and have it treated as an
        # HTTP credential.
        supplied_meta = params.get("_meta")
        if isinstance(supplied_meta, dict) and "authorization" in supplied_meta:
            return self._rpc_error(request_id, -32602,
                                   "authorization must be supplied as HTTP Bearer")
        try:
            if deadline is None:
                deadline = self.ctx.clock() + float(self.ctx.policy.limits["request_budget_s"])
            self.ctx.check_deadline(deadline)
            result = dispatch(self.ctx, name, arguments, deadline, token)
        except AdapterError as exc:
            if exc.code in {"auth_required", "auth_invalid"}:
                return _json_response(401, {"error": "invalid_token"}, self._challenge(invalid=True))
            if exc.code == "auth_insufficient_scope":
                scope = exc.details.get("required_scope", "t3:read")
                return _json_response(403, {"error": "insufficient_scope"},
                                      {"WWW-Authenticate": f'Bearer resource_metadata="{self.resource_metadata_url}", error="insufficient_scope", scope="{scope}"'})
            return _json_response(200, {"jsonrpc": "2.0", "id": request_id,
                                        "result": {"isError": True, "content": [{"type": "text", "text": json.dumps(exc.as_dict())}]}})
        return _json_response(200, {"jsonrpc": "2.0", "id": request_id,
                                    "result": {"structuredContent": result,
                                               "content": [{"type": "text", "text": json.dumps(result)}]}})

    def _rpc_error(self, request_id, code, message):
        return _json_response(400, {"jsonrpc": "2.0", "id": request_id,
                                    "error": {"code": code, "message": message}})

    def _tool_specs(self):
        specs = tool_specs(self.ctx.policy.allow_writes)
        for spec in specs:
            scope = "t3:write" if spec["name"].startswith(("t3_start", "t3_send", "t3_interrupt")) else "t3:read"
            spec["_meta"] = {"securitySchemes": [{"type": "oauth2", "scopes": [scope]}]}
        return specs


def create_app(ctx, **kwargs):
    """Return the bounded framework-neutral handler; do not bind a socket."""
    return MCPResourceServer(ctx, **kwargs).handle


def create_wsgi_app(ctx, **kwargs):
    """Return a bounded WSGI adapter around the resource server.

    The front server must preserve duplicate-header rejection and enforce
    request timeouts before WSGI normalization; ASGI is preferred when raw
    header fidelity is required.
    """
    server = MCPResourceServer(ctx, **kwargs)
    def application(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")
        headers = [(key[5:].replace("_", "-"), value)
                   for key, value in environ.items() if key.startswith("HTTP_")]
        if method == "GET" or path != server.mcp_path:
            body = b""
        else:
            if environ.get("HTTP_TRANSFER_ENCODING"):
                response = _json_response(501, {"error": "transfer_encoding_unsupported"})
                start_response(f"{response.status} Not Implemented", list(response.headers.items()))
                return [response.body]
            raw_length = environ.get("CONTENT_LENGTH")
            if (not isinstance(raw_length, str) or not raw_length or len(raw_length) > 20 or
                    any(char not in "0123456789" for char in raw_length)):
                response = _json_response(400, {"error": "content_length_required"})
                start_response(f"{response.status} Bad Request", list(response.headers.items()))
                return [response.body]
            try:
                length = int(raw_length, 10)
            except (TypeError, ValueError, OverflowError):
                response = _json_response(400, {"error": "content_length_required"})
                start_response(f"{response.status} Bad Request", list(response.headers.items()))
                return [response.body]
            if length > server.max_body_bytes:
                response = _json_response(413, {"error": "request_too_large"})
                start_response(f"{response.status} Request Entity Too Large", list(response.headers.items()))
                return [response.body]
            body = environ["wsgi.input"].read(length)
            if len(body) != length:
                response = _json_response(400, {"error": "short_request_body"})
                start_response(f"{response.status} Bad Request", list(response.headers.items()))
                return [response.body]
        response = server.handle(method, path, headers, body)
        start_response(f"{response.status} OK", list(response.headers.items()))
        return [response.body]
    return application


def create_asgi_app(ctx, *, shutdown_grace_s=1.0, **kwargs):
    """Return a single-worker, bounded ASGI adapter.

    One shared context may own mutable upstream sessions, so exactly one
    handler runs at a time. No executor queue is exposed: overload is rejected.
    Cancellation/timeouts stop waiting, not the underlying synchronous work.
    Hosts must drive lifespan shutdown (or await application.aclose()).
    """
    if not math.isfinite(shutdown_grace_s) or shutdown_grace_s < 0:
        raise ValueError("shutdown_grace_s must be finite and nonnegative")
    server = MCPResourceServer(ctx, **kwargs)
    executor = None
    active = set()
    stopping = False
    owner_loop = None

    def bind_loop():
        nonlocal owner_loop
        loop = asyncio.get_running_loop()
        if owner_loop is None:
            owner_loop = loop
        elif owner_loop is not loop:
            raise RuntimeError("ASGI application belongs to another event loop")
        return loop

    async def close():
        nonlocal stopping
        bind_loop()
        stopping = True
        try:
            if active:
                # asyncio.wait does not cancel the underlying jobs on timeout.
                await asyncio.wait(tuple(active), timeout=shutdown_grace_s)
            return not active
        finally:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

    async def dispatch_request(method, path, headers, body, deadline, wait_s):
        nonlocal executor
        loop = bind_loop()
        if stopping or active:
            return _json_response(503, {"error": "server_busy"}, {"Retry-After": "1"})
        if wait_s <= 0:
            return _json_response(408, {"error": "request_timeout"})
        if executor is None:
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="t3mcp")
        # Admission and submission contain no await. The slot is retired only
        # when the underlying job completes, never when its caller disappears.
        job = loop.run_in_executor(executor, lambda: server.handle(
            method, path, headers, body, deadline=deadline))
        active.add(job)
        def completed(future):
            active.discard(future)
            if not future.cancelled():
                future.exception()  # retrieve errors even after caller cancellation
        job.add_done_callback(completed)
        try:
            # ``asyncio.wait_for(asyncio.shield(job), ...)`` can fail to
            # observe completion of a run_in_executor future on Python 3.14
            # when the worker crosses a threading.Event boundary: the event
            # loop waits out the whole timeout even though the worker has
            # returned.  asyncio.wait does not cancel the running future on a
            # timeout or caller cancellation, so it preserves the capacity
            # invariant while reliably observing the completion.
            done, _ = await asyncio.wait((job,), timeout=wait_s)
            if not done:
                return _json_response(504, {"error": "handler_timeout"})
            try:
                return job.result()
            except Exception:
                # A backend exception does not establish whether an upstream
                # write took effect. Return only a generic availability error;
                # never expose the exception or claim the operation failed.
                return _json_response(503, {"error": "backend_unavailable"})
        except asyncio.CancelledError:
            raise

    async def application(scope, receive, send):
        loop = bind_loop()
        if scope.get("type") == "lifespan":
            message = await receive()
            if message.get("type") == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
                message = await receive()
            if message.get("type") == "lifespan.shutdown":
                drained = await close()
                if drained:
                    await send({"type": "lifespan.shutdown.complete"})
                else:
                    await send({"type": "lifespan.shutdown.failed",
                                "message": "Synchronous handler did not drain within shutdown grace"})
            return
        if scope.get("type") != "http":
            return
        # Both clocks are captured once at ingress, before reading any body.
        request_budget = float(server.ctx.policy.limits["request_budget_s"])
        deadline = server.ctx.clock() + request_budget
        response_deadline = loop.time() + request_budget
        ingress_deadline = min(response_deadline, loop.time() +
                               float(server.ctx.policy.limits["call_timeout_s"]))
        headers, declared_length, header_error = _asgi_headers(
            scope.get("headers", []), server.max_header_bytes, server.max_body_bytes)
        if header_error is not None:
            await send({"type": "http.response.start", "status": header_error.status,
                        "headers": [(k.lower().encode(), v.encode()) for k, v in header_error.headers.items()]})
            await send({"type": "http.response.body", "body": header_error.body})
            return
        body = bytearray()
        try:
            while True:
                remaining = ingress_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    response = _json_response(408, {"error": "request_timeout"})
                    break
                message = await asyncio.wait_for(receive(), timeout=remaining)
                if message.get("type") == "http.disconnect":
                    return
                if message.get("type") != "http.request":
                    continue
                chunk = message.get("body", b"")
                if not isinstance(chunk, bytes) or len(body) + len(chunk) > server.max_body_bytes:
                    response = _json_response(413, {"error": "request_too_large"})
                    break
                body.extend(chunk)
                if declared_length is not None and len(body) > declared_length:
                    response = _json_response(400, {"error": "content_length_mismatch"})
                    break
                if not message.get("more_body", False):
                    if declared_length is not None and len(body) != declared_length:
                        response = _json_response(400, {"error": "content_length_mismatch"})
                        break
                    response = await dispatch_request(scope.get("method", "GET"),
                        scope.get("path", "/"), headers, bytes(body), deadline,
                        response_deadline - loop.time())
                    break
        except asyncio.TimeoutError:
            response = _json_response(408, {"error": "request_timeout"})
        except ConnectionError:
            return
        await send({"type": "http.response.start", "status": response.status,
                    "headers": [(k.lower().encode(), v.encode()) for k, v in response.headers.items()]})
        await send({"type": "http.response.body", "body": response.body})
    application.aclose = close
    return application

