"""Read-only Beeper HTTP resource boundary; no listener or OAuth server.

The gateway MUST reject duplicate headers before WSGI normalization, enforce
TLS, body/header/read/write timeouts and connection limits, and pass the real
Host without trusting client Forwarded headers. WSGI cannot enforce these.
"""
import json
import math
import threading
from http import HTTPStatus
from urllib.parse import urlsplit

from .beeper_mcp_plugin import BeeperMCPPlugin, READ_SCOPE, PROTOCOL_VERSION


def _url(value):
    if not isinstance(value, str) or not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("explicit ASCII HTTPS URL required")
    p = urlsplit(value)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.query or p.fragment \
            or any(c in value for c in ('"', '\\', '<', '>')):
        raise ValueError("invalid HTTPS URL")
    p.port  # validate port syntax/range
    return p


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("nonfinite JSON number")
    return parsed


def _constant(value):
    raise ValueError("nonfinite JSON number")


def create_secure_wsgi_app(plugin, *, authorization_server, allowed_origins=(),
                           max_request_bytes=1_000_000, max_header_bytes=16_384,
                           max_response_bytes=2_000_000, max_concurrent=1):
    """Return a bounded stateless JSON-only Streamable HTTP WSGI app.

    Native clients may omit Origin. Browser origins must be explicitly allowed;
    no CORS/preflight is provided. GET MCP returns 405 (no SSE stream).
    The gateway contract in this module remains mandatory.
    """
    if not isinstance(plugin, BeeperMCPPlugin):
        raise ValueError("configured Beeper plugin required")
    resource = plugin.server_resource
    parsed = _url(resource)
    _url(authorization_server)
    verifier = getattr(plugin.verify_caller, "jwt_verifier", None)
    if getattr(verifier, "issuer", None) != authorization_server \
            or getattr(verifier, "audience", None) != resource:
        raise ValueError("verified issuer and audience must match discovery")
    if not isinstance(allowed_origins, (list, tuple, set, frozenset)):
        raise ValueError("explicit origin collection required")
    origins = frozenset(allowed_origins)
    for origin in origins:
        op = _url(origin)
        if op.path:
            raise ValueError("origin must not include a path")
    for limit in (max_request_bytes, max_header_bytes, max_response_bytes, max_concurrent):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("positive integer limits required")
    if max_response_bytes < 64:
        raise ValueError("response limit must fit a bounded error response")
    path = parsed.path or "/"
    metadata_path = "/.well-known/oauth-protected-resource" + (path if path != "/" else "")
    metadata_url = f"https://{parsed.netloc}{metadata_path}"
    slots = threading.BoundedSemaphore(max_concurrent)

    def challenge(error=None, scope=False):
        value = f'Bearer resource_metadata="{metadata_url}"'
        if error:
            value += f', error="{error}"'
        if scope:
            value += f', scope="{READ_SCOPE}"'
        return value

    def response(start, status, payload=None, headers=None):
        body = b"" if payload is None else json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
        if len(body) > max_response_bytes:
            status, body = 502, b'{"error":"response_too_large"}'
        result = [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
                  ("Cache-Control", "no-store")]
        result.extend((headers or {}).items())
        start(f"{status} {HTTPStatus(status).phrase}", result)
        return [body]

    def application(environ, start_response):
        def fail(status, error, headers=None):
            return response(start_response, status, {"error": error}, headers)
        # Never trust X-Forwarded-Host or X-Forwarded-Proto supplied by clients.
        if environ.get("wsgi.url_scheme") != "https" or environ.get("HTTP_HOST") != parsed.netloc:
            return fail(421, "resource_mismatch")
        headers = {key: value for key, value in environ.items()
                   if key.startswith("HTTP_") or key in ("CONTENT_TYPE", "CONTENT_LENGTH")}
        # WSGI represents HTTP field bytes as Latin-1 strings. Reject control
        # characters and malformed server strings before body reads or UTF-8
        # size accounting can raise (for example on an unpaired surrogate).
        if any(not isinstance(v, str) or any(
                ord(c) > 255 or ord(c) == 127 or (ord(c) < 32 and c != '\t')
                for c in v) for v in headers.values()):
            return fail(400, "invalid_header")
        if sum(len(k.encode()) + len(v.encode()) for k, v in headers.items()) > max_header_bytes:
            return fail(431, "headers_too_large")
        origin = environ.get("HTTP_ORIGIN")
        if origin is not None and origin not in origins:
            return fail(403, "origin_denied")
        if environ.get("QUERY_STRING") or environ.get("SCRIPT_NAME"):
            return fail(404, "not_found")
        route = environ.get("PATH_INFO")
        method = environ.get("REQUEST_METHOD")
        if route == metadata_path and method == "GET":
            return response(start_response, 200, {"resource": resource,
                "authorization_servers": [authorization_server],
                "scopes_supported": [READ_SCOPE], "bearer_methods_supported": ["header"]})
        if route != path:
            return fail(404, "not_found")
        if method != "POST":
            return fail(405, "method_not_allowed", {"Allow": "POST"})
        if environ.get("HTTP_TRANSFER_ENCODING") is not None:
            return fail(400, "transfer_encoding_not_supported")
        length = environ.get("CONTENT_LENGTH", "")
        if not length or len(length) > 10 or any(c not in "0123456789" for c in length):
            return fail(400, "content_length_required")
        length = int(length)
        if length > max_request_bytes:
            return fail(413, "request_too_large")
        if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
            return fail(415, "unsupported_media_type")
        accept = {part.split(";", 1)[0].strip().lower() for part in environ.get("HTTP_ACCEPT", "").split(",")}
        if not {"application/json", "text/event-stream"}.issubset(accept):
            return fail(406, "unsupported_accept")
        version = environ.get("HTTP_MCP_PROTOCOL_VERSION")
        if version is not None and version != PROTOCOL_VERSION:
            return fail(400, "unsupported_protocol_version")
        if not slots.acquire(blocking=False):
            return fail(503, "server_busy", {"Retry-After": "1"})
        try:
            stream = environ.get("wsgi.input")
            try:
                raw = stream.read(length)
            except Exception:
                return fail(400, "invalid_request_body")
            if not isinstance(raw, bytes) or len(raw) != length:
                return fail(400, "short_request_body")
            try:
                request = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant,
                                     parse_float=_float)
            except (ValueError, UnicodeError, RecursionError):
                return fail(400, "invalid_json")
            if not isinstance(request, dict):
                return fail(400, "invalid_jsonrpc")
            # Notifications never receive JSON-RPC responses. Only the
            # initialized notification is supported; rejected notifications
            # use an HTTP error, not a JSON-RPC response with a null ID.
            if "id" not in request and request.get("method") != "notifications/initialized":
                return fail(400, "unsupported_or_invalid_notification")
            if request.get("method") == "notifications/initialized" and (
                    "id" in request or request.get("jsonrpc") != "2.0" or
                    not isinstance(request.get("params", {}), dict)):
                return fail(400, "invalid_notification")
            if request.get("method") == "initialize":
                params = request.get("params")
                if not isinstance(params, dict) or not isinstance(params.get("protocolVersion"), str) \
                        or not isinstance(params.get("capabilities"), dict) \
                        or not isinstance(params.get("clientInfo"), dict) \
                        or not all(isinstance(params["clientInfo"].get(k), str) and params["clientInfo"][k]
                                   for k in ("name", "version")):
                    return fail(400, "invalid_initialize")
            elif version is None:
                # Stateless server supports the March 2025 protocol fallback.
                version = PROTOCOL_VERSION
            auth = environ.get("HTTP_AUTHORIZATION")
            result = plugin.handle(request, auth)
            extra = {k:v for k,v in result.headers.items() if k.lower() not in ("cache-control", "www-authenticate")}
            if result.status == 401:
                extra["WWW-Authenticate"] = challenge("invalid_token" if auth else None)
            elif result.status == 403 and "WWW-Authenticate" in result.headers:
                extra["WWW-Authenticate"] = challenge("insufficient_scope", scope=True)
            return response(start_response, result.status, result.body, extra)
        finally:
            slots.release()

    return application
