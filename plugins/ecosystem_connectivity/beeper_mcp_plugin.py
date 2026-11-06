"""Bounded MCP-facing facade for the read-only Beeper client.

This module is intentionally a protocol/auth boundary, not an HTTP listener.
The host must provide an OAuth/OIDC resource-server verifier backed by an
established authorization server and a separate runtime Beeper token provider.
There is no default verifier, token minting, HMAC fallback, send tool, or
network server in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import hmac
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from .beeper_mcp_readonly import BeeperMCPError


class BeeperPluginError(RuntimeError):
    pass


class CallerUnauthorized(BeeperPluginError):
    pass


class CallerForbidden(BeeperPluginError):
    pass


class CallerInsufficientScope(CallerForbidden):
    pass


@dataclass(frozen=True)
class Caller:
    subject: str
    scopes: frozenset[str]
    allowed_chats: frozenset[str]


class JwtCallerVerifier:
    """Bind the audited T3 JWT verifier to per-subject chat entitlements."""

    def __init__(self, jwt_verifier, chat_entitlements, *, resource):
        if not callable(getattr(jwt_verifier, "verify", None)) \
                or not isinstance(chat_entitlements, Mapping) \
                or not isinstance(resource, str) or not resource:
            raise BeeperPluginError("JWT verifier and chat entitlements are required")
        if getattr(jwt_verifier, "audience", None) != resource:
            raise BeeperPluginError("JWT audience must equal the MCP resource")
        self.jwt_verifier = jwt_verifier
        self.chat_entitlements = chat_entitlements
        self.resource = resource

    def __call__(self, token):
        auth = self.jwt_verifier.verify(token)
        subject = getattr(auth, "subject", None)
        scopes = getattr(auth, "scopes", None)
        chats = self.chat_entitlements.get(subject)
        if not isinstance(subject, str) or not subject \
                or not isinstance(scopes, (set, frozenset)) \
                or not isinstance(chats, (set, frozenset, list, tuple)) \
                or any(not isinstance(chat, str) or not chat for chat in chats):
            raise CallerForbidden("caller has no configured chat entitlement")
        return Caller(subject, frozenset(scopes), frozenset(chats))


@dataclass(frozen=True)
class PluginResponse:
    status: int
    headers: Mapping[str, str]
    body: dict[str, Any] | None


READ_TOOLS = frozenset(("search_chats", "list_messages"))
READ_SCOPE = "beeper:read"
PROTOCOL_VERSION = "2025-03-26"
MAX_REQUEST_BYTES = 1_000_000


class BeeperMCPPlugin:
    """Stateless JSON-RPC MCP facade with fail-closed caller boundaries.

    ``verify_caller`` must validate an external OAuth/OIDC access token,
    including issuer, signature/introspection, expiry, audience/resource, and
    scopes, then return a Caller. ``beeper_token_provider`` supplies a separate
    token for the local Beeper Desktop MCP route; the caller token is never
    forwarded. ``reader_factory`` must return a fresh ReadOnlyBeeperMCP client
    configured with the caller's allowed chats.
    """

    def __init__(self, *, verify_caller: Callable[[str], Caller] | None,
                 beeper_token_provider: Callable[[Caller], str] | None,
                 reader_factory: Callable[[str, frozenset[str]], Any] | None,
                 server_resource: str):
        if not callable(verify_caller) or not callable(beeper_token_provider) \
                or not callable(reader_factory):
            raise BeeperPluginError("external verifier, token provider, and reader factory are required")
        parsed = urlparse(server_resource) if isinstance(server_resource, str) else None
        if parsed is None or parsed.scheme not in ("https", "http") \
                or not parsed.hostname \
                or (parsed.scheme == "http" and parsed.hostname != "localhost"):
            raise BeeperPluginError("server resource must be an explicit HTTPS resource or localhost")
        verifier_resource = getattr(verify_caller, "resource", None)
        if verifier_resource is not None and verifier_resource != server_resource:
            raise BeeperPluginError("JWT audience must equal the MCP resource")
        self.verify_caller = verify_caller
        self.beeper_token_provider = beeper_token_provider
        self.reader_factory = reader_factory
        self.server_resource = server_resource

    @staticmethod
    def _error(request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": code, "message": message}}

    @staticmethod
    def _auth_token(header):
        if not isinstance(header, str) or not header.startswith("Bearer "):
            raise CallerUnauthorized("missing bearer token")
        token = header[7:]
        if not token or any(char.isspace() for char in token):
            raise CallerUnauthorized("invalid bearer token")
        return token

    def _authenticate(self, header):
        token = self._auth_token(header)
        try:
            caller = self.verify_caller(token)
        except CallerForbidden:
            raise
        except Exception as exc:
            raise CallerUnauthorized("access token rejected") from exc
        if not isinstance(caller, Caller) or not isinstance(caller.subject, str) \
                or not caller.subject or not isinstance(caller.scopes, (set, frozenset)) \
                or any(not isinstance(scope, str) for scope in caller.scopes) \
                or not isinstance(caller.allowed_chats, (set, frozenset)) \
                or any(not isinstance(chat, str) or not chat for chat in caller.allowed_chats):
            raise CallerForbidden("invalid caller entitlement")
        if READ_SCOPE not in caller.scopes:
            raise CallerInsufficientScope("read scope is required")
        return caller

    @staticmethod
    def _tools():
        tools = [
            {"name": "search_chats", "description": "Search allowlisted Beeper chats.",
             "inputSchema": {"type": "object", "properties": {
                 "query": {"type": "string", "maxLength": 256},
                 "scope": {"type": "string", "enum": ["titles", "participants"]},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
                 "required": ["query"], "additionalProperties": False}},
            {"name": "list_messages", "description": "List messages from one allowlisted chat.",
             "inputSchema": {"type": "object", "properties": {
                 "chatID": {"type": "string", "maxLength": 256},
                 "cursor": {"type": "string", "maxLength": 256},
                 "direction": {"type": "string", "enum": ["after", "before"]}},
                 "required": ["chatID"], "additionalProperties": False}},
        ]
        for tool in tools:
            # Both declarations are intentional: some hosts read only _meta.
            # These hints describe the surface, never authorize a request.
            tool["securitySchemes"] = [{"type": "oauth2", "scopes": [READ_SCOPE]}]
            tool["_meta"] = {"securitySchemes": [
                {"type": "oauth2", "scopes": [READ_SCOPE]}]}
            tool["annotations"] = {
                "readOnlyHint": True, "destructiveHint": False,
                "openWorldHint": False, "idempotentHint": True,
            }
        return tools

    def _call_read(self, caller, name, arguments, caller_token=None):
        if name not in READ_TOOLS:
            raise CallerForbidden("tool is not available")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        args = dict(arguments)
        if name == "list_messages":
            chat_id = args.get("chatID", args.get("chat_id"))
            if "chatID" in args and "chat_id" in args and args["chatID"] != args["chat_id"]:
                raise ValueError("conflicting chat identities")
            if not isinstance(chat_id, str) or chat_id not in caller.allowed_chats:
                raise CallerForbidden("chat is not allowlisted")
            args = {"chatID": chat_id, **{key: value for key, value in args.items()
                                          if key not in ("chatID", "chat_id")}}
        token = self.beeper_token_provider(caller)
        if not isinstance(token, str) or not token or (caller_token is not None
                and hmac.compare_digest(token.encode(), caller_token.encode())):
            raise BeeperPluginError("Beeper token provider failed closed")
        reader = self.reader_factory(token, caller.allowed_chats)
        if reader is None:
            raise BeeperPluginError("reader factory failed closed")
        if getattr(reader, "allowed_chats", None) != caller.allowed_chats:
            raise BeeperPluginError("reader chat policy does not match caller")
        reader.initialize(token)
        reader.list_tools()
        return reader.call_read_tool(name, args)

    def handle(self, request, authorization_header) -> PluginResponse:
        request_id = request.get("id") if isinstance(request, dict) else None
        # Never reflect attacker-controlled containers into an auth error. Apart
        # from invalid JSON-RPC IDs, they can exhaust the response encoder's stack.
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            request_id = None
        try:
            caller = self._authenticate(authorization_header)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" \
                    or not isinstance(request.get("method"), str):
                raise ValueError("invalid JSON-RPC request")
            method = request["method"]
            if method == "notifications/initialized":
                if "id" in request:
                    raise ValueError("notification must not have an id")
            elif "id" not in request or isinstance(request.get("id"), bool) \
                    or not isinstance(request.get("id"), (str, int)):
                raise ValueError("request id is required")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            if method == "initialize":
                result = {"protocolVersion": PROTOCOL_VERSION,
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "dot-kit-beeper-readonly", "version": "0.1"}}
            elif method == "notifications/initialized":
                return PluginResponse(202, {"Cache-Control": "no-store"}, None)
            elif method == "tools/list":
                result = {"tools": self._tools()}
            elif method == "tools/call":
                name = params.get("name")
                if not isinstance(name, str):
                    raise ValueError("tool name is required")
                result = self._call_read(caller, name, params.get("arguments", {}),
                                         self._auth_token(authorization_header))
            else:
                return PluginResponse(200, {"Cache-Control": "no-store"},
                                      self._error(request_id, -32601, "method not found"))
            return PluginResponse(200, {"Cache-Control": "no-store"},
                                  {"jsonrpc": "2.0", "id": request_id, "result": result})
        except CallerUnauthorized:
            return PluginResponse(401, {"WWW-Authenticate": f'Bearer resource="{self.server_resource}"',
                                        "Cache-Control": "no-store"},
                                  self._error(request_id, -32001, "unauthorized"))
        except CallerInsufficientScope:
            return PluginResponse(403, {"Cache-Control": "no-store",
                                      "WWW-Authenticate": 'Bearer error="insufficient_scope", scope="beeper:read"'},
                                  self._error(request_id, -32003, "insufficient scope"))
        except CallerForbidden:
            return PluginResponse(403, {"Cache-Control": "no-store"},
                                  self._error(request_id, -32003, "forbidden"))
        except (ValueError, BeeperMCPError):
            return PluginResponse(200, {"Cache-Control": "no-store"},
                                  self._error(request_id, -32602, "invalid request or tool arguments"))
        except Exception:
            return PluginResponse(200, {"Cache-Control": "no-store"},
                                  self._error(request_id, -32000, "backend unavailable"))


def create_beeper_wsgi_app(plugin, *, max_request_bytes=MAX_REQUEST_BYTES):
    """Create a bounded WSGI app; deployment supplies TLS and the server.

    This factory does not bind a port, start a server, add CORS, or create
    credentials. A real deployment must place it behind HTTPS and configure
    the external authorization verifier and Beeper token provider passed to
    ``BeeperMCPPlugin``. The host must enforce the configured resource's exact
    Host/path, reject untrusted Origins, validate MCP-Protocol-Version, and
    bound header sizes, body-read time and concurrency before calling this app.
    This factory is not a complete Streamable HTTP deployment boundary; see
    plugins/ecosystem-connectivity/README.md for the required gateway contract.
    """
    if not isinstance(plugin, BeeperMCPPlugin):
        raise BeeperPluginError("a configured BeeperMCPPlugin is required")
    if isinstance(max_request_bytes, bool) or not isinstance(max_request_bytes, int) \
            or max_request_bytes <= 0:
        raise BeeperPluginError("invalid request limit")

    def app(environ, start_response):
        method = environ.get("REQUEST_METHOD")
        if method != "POST":
            body = b"method not allowed"
            start_response("405 Method Not Allowed", [("Content-Type", "text/plain"),
                                                        ("Content-Length", str(len(body))),
                                                        ("Cache-Control", "no-store")])
            return [body]
        content_type = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            body = b"unsupported media type"
            start_response("415 Unsupported Media Type", [("Content-Type", "text/plain"),
                                                            ("Content-Length", str(len(body))),
                                                            ("Cache-Control", "no-store")])
            return [body]
        try:
            content_length = int(environ.get("CONTENT_LENGTH", "-1"))
        except (TypeError, ValueError):
            content_length = -1
        if content_length < 0 or content_length > max_request_bytes:
            body = b"invalid request size"
            start_response("413 Request Entity Too Large", [("Content-Type", "text/plain"),
                                                              ("Content-Length", str(len(body))),
                                                              ("Cache-Control", "no-store")])
            return [body]
        raw = environ.get("wsgi.input").read(content_length)
        if not isinstance(raw, bytes) or len(raw) != content_length:
            body = b"invalid request body"
            start_response("400 Bad Request", [("Content-Type", "text/plain"),
                                                ("Content-Length", str(len(body))),
                                                ("Cache-Control", "no-store")])
            return [body]
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            request = None
        response = plugin.handle(request, environ.get("HTTP_AUTHORIZATION"))
        body = b"" if response.body is None else json.dumps(
            response.body, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        headers = [("Content-Type", "application/json"),
                   ("Content-Length", str(len(body)))]
        headers.extend((str(key), str(value)) for key, value in response.headers.items())
        start_response(f"{response.status} {'OK' if response.status < 300 else 'Error'}", headers)
        return [body]

    return app


