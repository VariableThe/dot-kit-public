"""Read-only client for Beeper Desktop's documented Streamable HTTP MCP route.

The bearer token is supplied at call time and never persisted. This client
does not expose send or mutation calls. Live compatibility still requires a
runtime protocol check against the installed Beeper Desktop version.
"""

from __future__ import annotations

import json
import http.client
import re
from urllib.parse import urlparse, urlunparse, quote
import urllib.error
import urllib.request


class BeeperMCPError(RuntimeError):
    pass


DEFAULT_READ_TOOLS = frozenset(("search_chats", "list_messages"))
SUPPORTED_PROTOCOL_VERSIONS = frozenset(("2024-11-05", "2025-03-26", "2025-06-18"))

_READ_SCHEMA_REQUIREMENTS = {
    "search_chats": ("query",),
    "list_messages": ("chatID", "chat_id"),
}
_READ_SCHEMA_FIELDS = {
    "search_chats": frozenset(("query", "scope", "limit")),
    "list_messages": frozenset(("chatID", "chat_id", "cursor", "direction")),
}
_CHAT_CARD_RE = re.compile(r"^## (.+?) \(chatID: ([^)]+)\)\s*$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class StreamableHTTPTransport:
    def __init__(self, url, *, max_response_bytes=2_000_000, opener=None):
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme not in ("http", "https") \
                or parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise BeeperMCPError("MCP URL must be http(s)")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) \
                or max_response_bytes <= 0:
            raise BeeperMCPError("invalid response limit")
        self.url = url
        self.max_response_bytes = max_response_bytes
        # Loopback credentials must never follow ambient HTTP(S)_PROXY
        # settings. An empty ProxyHandler suppresses urllib's default proxy
        # discovery, independently of NO_PROXY configuration.
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())
        self.session_id = None

    def post(self, payload, token):
        if not isinstance(token, str) or not token:
            raise BeeperMCPError("bearer token is required at runtime")
        body = json.dumps(payload, separators=(",", ":")).encode()
        request = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "Authorization": f"Bearer {token}"},
        )
        if getattr(self, "protocol_version", None):
            request.add_header("MCP-Protocol-Version", self.protocol_version)
        if self.session_id:
            request.add_header("Mcp-Session-Id", self.session_id)
        try:
            opener_call = self.opener.open if hasattr(self.opener, "open") else self.opener
            with opener_call(request, timeout=20) as response:
                raw = response.read(self.max_response_bytes + 1)
                status = getattr(response, "status", 200)
                session_id = response.headers.get("Mcp-Session-Id")
        except (OSError, urllib.error.URLError) as exc:
            raise BeeperMCPError("MCP transport failed") from exc
        if len(raw) > self.max_response_bytes:
            raise BeeperMCPError("MCP response exceeds configured limit")
        if status < 200 or status >= 300:
            raise BeeperMCPError("MCP authorization or protocol request failed")
        if session_id:
            self.session_id = session_id
        return self._decode(raw, payload.get("id"))

    def notify(self, payload, token):
        if not isinstance(token, str) or not token:
            raise BeeperMCPError("bearer token is required at runtime")
        request = urllib.request.Request(
            self.url, data=json.dumps(payload, separators=(",", ":")).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "Accept": "application/json, text/event-stream",
                                    "Authorization": f"Bearer {token}"},
        )
        if getattr(self, "protocol_version", None):
            request.add_header("MCP-Protocol-Version", self.protocol_version)
        if self.session_id:
            request.add_header("Mcp-Session-Id", self.session_id)
        try:
            opener_call = self.opener.open if hasattr(self.opener, "open") else self.opener
            with opener_call(request, timeout=20) as response:
                if response.status < 200 or response.status >= 300:
                    raise BeeperMCPError("MCP notification failed")
        except (OSError, urllib.error.URLError) as exc:
            raise BeeperMCPError("MCP transport failed") from exc

    def resolve_chat_identity(self, requested_id, token):
        """Read one same-instance identity pair; discard all other metadata.

        No cache or state survives this request. The REST route is independent
        of MCP sessions but uses the identical origin and backend credential.
        """
        if not isinstance(requested_id, str) or not requested_id or len(requested_id) > 256 \
                or requested_id in (".", ".."):
            raise BeeperMCPError("invalid chat identity")
        if not isinstance(token, str) or not token:
            raise BeeperMCPError("bearer token is required at runtime")
        origin = urlparse(self.url)
        if origin.username is not None or origin.password is not None:
            raise BeeperMCPError("credential-bearing endpoint is forbidden")
        url = urlunparse((origin.scheme, origin.netloc,
                         "/v1/chats/" + quote(requested_id, safe=""), "",
                         "maxParticipantCount=0", ""))
        request = urllib.request.Request(url, method="GET", headers={
            "Accept": "application/json", "Authorization": f"Bearer {token}"})
        try:
            opener_call = self.opener.open if hasattr(self.opener, "open") else self.opener
            with opener_call(request, timeout=20) as response:
                status = getattr(response, "status", 200)
                raw = response.read(self.max_response_bytes + 1)
            if status < 200 or status >= 300 or len(raw) > self.max_response_bytes:
                raise BeeperMCPError("chat identity lookup failed")
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate JSON field")
                    result[key] = value
                return result
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
        except (OSError, urllib.error.URLError, http.client.HTTPException,
                ValueError, UnicodeError, RecursionError):
            # Do not propagate URLs, response bodies, or parser documents in
            # an exception chain that a host might log.
            raise BeeperMCPError("chat identity lookup failed") from None
        return _verified_identity_pair(payload, requested_id)

    @staticmethod
    def _decode(raw, expected_id=None):
        try:
            text = raw.decode("utf-8").replace("\r\n", "\n")
            if "data:" in text and "\n\n" in text:
                events = []
                for chunk in text.split("\n\n"):
                    event_id = None
                    data_lines = []
                    for line in chunk.splitlines():
                        if line.startswith("id:"):
                            event_id = line[3:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                    if data_lines:
                        events.append((event_id, "\n".join(data_lines)))
                if not events:
                    raise ValueError("empty SSE response")
                values = [(event_id, json.loads(data)) for event_id, data in events]
                value = next((item for _event_id, item in values
                              if expected_id is None or item.get("id") == expected_id), None)
                if value is None:
                    raise ValueError("no matching SSE response")
            else:
                value = json.loads(text)
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise BeeperMCPError("invalid MCP JSON response") from exc
        if not isinstance(value, dict) or ("error" not in value and "result" not in value):
            raise BeeperMCPError("invalid MCP response envelope")
        if "error" in value:
            raise BeeperMCPError("MCP returned an error")
        return value


def _verified_identity_pair(payload, requested_id):
    if not isinstance(payload, dict):
        raise BeeperMCPError("invalid chat identity metadata")
    canonical = payload.get("id")
    local = payload.get("localChatID")
    if any(not isinstance(value, str) or not value or len(value) > 256
           for value in (canonical, local)):
        raise BeeperMCPError("missing chat identity metadata")
    if requested_id != local:
        raise BeeperMCPError("chat identity metadata does not match request")
    # Never follow merge relationships or alternate identity fields implicitly.
    if payload.get("merge") is not None or payload.get("mergedIntoChatID") is not None:
        raise BeeperMCPError("merged chat identity is unsupported")
    for alias in ("chatID", "chat_id"):
        if alias in payload and payload[alias] != canonical:
            raise BeeperMCPError("conflicting chat identity metadata")
    return {"id": canonical, "localChatID": local}


class ReadOnlyBeeperMCP:
    def __init__(self, transport, *, protocol_version="2025-03-26",
                 read_tool_allowlist=DEFAULT_READ_TOOLS, allowed_chats=None,
                 max_items=50):
        if not isinstance(read_tool_allowlist, (set, frozenset, tuple, list)) \
                or not read_tool_allowlist:
            raise BeeperMCPError("read tool allowlist is required")
        if not set(read_tool_allowlist).issubset(DEFAULT_READ_TOOLS):
            raise BeeperMCPError("allowlist contains an unreviewed or mutating tool")
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
            raise BeeperMCPError("invalid item limit")
        self.transport = transport
        self.protocol_version = protocol_version
        self.read_tool_allowlist = frozenset(read_tool_allowlist)
        self.allowed_chats = frozenset(allowed_chats or ())
        self.max_items = max_items
        self._schemas = None
        self._next_id = 1
        self._initialized = False

    def _request(self, method, params):
        request_id = self._next_id
        self._next_id += 1
        response = self.transport.post(
            {"jsonrpc": "2.0", "id": request_id, "method": method,
             "params": params}, self.transport_token)
        if response.get("id") != request_id or not isinstance(response.get("result"), dict):
            raise BeeperMCPError("invalid MCP result")
        return response["result"]

    def initialize(self, token):
        self._reset_state()
        self.transport_token = token
        try:
            result = self._request("initialize", {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "dot-kit-readonly", "version": "0.1"},
            })
            if result.get("protocolVersion") not in SUPPORTED_PROTOCOL_VERSIONS:
                raise BeeperMCPError("MCP initialize response lacks protocol version")
            self.transport.protocol_version = result["protocolVersion"]
            notification = {"jsonrpc": "2.0", "method": "notifications/initialized",
                            "params": {}}
            if hasattr(self.transport, "notify"):
                self.transport.notify(notification, self.transport_token)
            else:
                self.transport.post(notification, self.transport_token)
            self._initialized = True
            return {"protocolVersion": result["protocolVersion"],
                    "serverInfo": result.get("serverInfo")}
        except Exception:
            self._reset_state()
            raise

    def _reset_state(self):
        self._initialized = False
        self._schemas = None
        self.transport_token = None
        if hasattr(self.transport, "session_id"):
            self.transport.session_id = None
        if hasattr(self.transport, "protocol_version"):
            self.transport.protocol_version = None

    def list_tools(self, token=None):
        self._schemas = None
        if token is not None:
            self.transport_token = token
        if not self._initialized:
            raise BeeperMCPError("MCP client is not initialized")
        result = self._request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
            raise BeeperMCPError("invalid MCP tools/list result")
        schemas = {}
        try:
            for tool in tools:
                name = tool.get("name")
                if name in self.read_tool_allowlist:
                    schema = tool.get("inputSchema")
                    if not self._schema_is_compatible(name, schema):
                        raise BeeperMCPError("read tool schema is missing or incompatible")
                    schemas[name] = schema
            missing = self.read_tool_allowlist - schemas.keys()
            if missing:
                raise BeeperMCPError("installed server did not advertise the reviewed read tools")
            self._schemas = schemas
            return tools
        except Exception:
            self._schemas = None
            raise

    @staticmethod
    def _schema_is_compatible(name, schema):
        """Require enough schema to validate the wrapper's input contract."""
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return False
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return False
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            return False
        names = _READ_SCHEMA_REQUIREMENTS[name]
        if any(item not in _READ_SCHEMA_FIELDS[name] for item in required):
            return False
        if name == "search_chats":
            query = properties.get("query")
            return isinstance(query, dict) and query.get("type") == "string"
        return any(isinstance(properties.get(candidate), dict)
                   and properties[candidate].get("type") == "string"
                   for candidate in names)

    def _validated_arguments(self, name, arguments):
        schema = self._schemas[name]
        properties = schema["properties"]
        args = dict(arguments)
        if "chatID" in args and "chat_id" in args:
            if args["chatID"] != args["chat_id"]:
                raise BeeperMCPError("conflicting input chat identities")
            args.pop("chat_id")
        if name == "list_messages":
            if "chatID" in properties and "chat_id" not in properties and "chat_id" in args:
                args["chatID"] = args.pop("chat_id")
            elif "chat_id" in properties and "chatID" not in properties and "chatID" in args:
                args["chat_id"] = args.pop("chatID")
        for key, value in args.items():
            if key not in properties or key not in _READ_SCHEMA_FIELDS[name]:
                raise BeeperMCPError("tool arguments are not supported by discovered schema")
            expected = properties[key].get("type") if isinstance(properties[key], dict) else None
            if expected == "string" and not isinstance(value, str):
                raise BeeperMCPError("tool argument has the wrong type")
            if expected == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
                raise BeeperMCPError("tool argument has the wrong type")
            if expected not in ("string", "integer"):
                raise BeeperMCPError("unsupported discovered argument type")
            enum = properties[key].get("enum")
            if enum is not None and (not isinstance(enum, list) or value not in enum):
                raise BeeperMCPError("tool argument is outside discovered enum")
            if key in ("query", "scope", "cursor") and len(value) > 256:
                raise BeeperMCPError("tool argument is too long")
            if key == "limit" and (value <= 0 or value > self.max_items):
                raise BeeperMCPError("tool limit is out of bounds")
        required = schema.get("required", [])
        if any(key not in args for key in required):
            raise BeeperMCPError("required tool argument is missing")
        return args

    def _parse_search_payload(self, text):
        try:
            return json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            if len(text) > 20000:
                raise BeeperMCPError("search result is too large")
            rows = []
            for line in text.splitlines():
                if line.strip() == "# Messages":
                    break
                if not line.strip().startswith("## "):
                    continue
                match = _CHAT_CARD_RE.match(line.strip())
                if not match:
                    raise BeeperMCPError("malformed search chat card")
                rows.append({"chat_id": match.group(2), "title": match.group(1)})
            if not rows:
                raise BeeperMCPError("search result is neither JSON nor chat cards")
            return {"chats": rows}

    def _parse_message_payload(self, text, chat_id):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BeeperMCPError("read tool returned unstructured content") from exc
        if not isinstance(payload, dict):
            raise BeeperMCPError("invalid message result")
        if not isinstance(payload.get("items", []), list):
            raise BeeperMCPError("invalid message items")
        if "items" not in payload:
            return payload
        top_chat = payload.get("chatID", payload.get("chat_id"))
        if "chatID" in payload and "chat_id" in payload and payload["chatID"] != payload["chat_id"]:
            raise BeeperMCPError("conflicting top-level chat identities")
        # Beeper's captured page envelope has no top-level chat identity.
        # In that case require a nonempty page and explicit matching identity
        # on every item; never infer an empty page's identity from the request.
        if top_chat is None and not payload["items"]:
            raise BeeperMCPError("empty message result lacks chat identity")
        if top_chat is not None and top_chat != chat_id:
            raise BeeperMCPError("messages returned for a disallowed chat")
        has_more = payload.get("hasMore")
        oldest = payload.get("oldestCursor")
        if not isinstance(has_more, bool) or (oldest is not None and
                                               (not isinstance(oldest, str) or len(oldest) > 256)):
            raise BeeperMCPError("invalid message pagination")
        messages = []
        for item in payload["items"]:
            if not isinstance(item, dict):
                raise BeeperMCPError("invalid message item")
            row_chat_id = item.get("chat_id")
            row_chatID = item.get("chatID")
            if row_chat_id is not None and row_chatID is not None and row_chat_id != row_chatID:
                raise BeeperMCPError("conflicting message chat identities")
            row_chat = row_chat_id if row_chat_id is not None else row_chatID
            if top_chat is None and row_chat != chat_id:
                raise BeeperMCPError("message item lacks matching chat identity")
            if row_chat not in (None, chat_id):
                raise BeeperMCPError("message crossed chat boundary")
            mid, body = item.get("id"), item.get("text", "")
            if not isinstance(mid, str) or not mid or len(mid) > 256 \
                    or not isinstance(body, str) or len(body) > 20000:
                raise BeeperMCPError("invalid message item")
            messages.append({"id": mid, "text": body})
        return {"chat_id": chat_id, "messages": messages,
                "has_more": has_more, "oldest_cursor": oldest}

    def call_read_tool(self, name, arguments):
        if not self._initialized:
            raise BeeperMCPError("MCP client is not initialized")
        if not isinstance(name, str) or name not in self.read_tool_allowlist:
            raise BeeperMCPError("mutating Beeper tools are disabled")
        if self._schemas is None or name not in self._schemas:
            raise BeeperMCPError("tools/list discovery is required before reads")
        if not isinstance(arguments, dict):
            raise BeeperMCPError("tool arguments must be an object")
        arguments = self._validated_arguments(name, arguments)
        chat_id = arguments.get("chatID", arguments.get("chat_id"))
        if name == "list_messages" and chat_id not in self.allowed_chats:
            raise BeeperMCPError("chat is not allowlisted")
        requested_chat_id = chat_id
        if name == "list_messages" and hasattr(self.transport, "resolve_chat_identity"):
            pair = self.transport.resolve_chat_identity(chat_id, self.transport_token)
            pair = _verified_identity_pair(pair, chat_id)
            chat_id = pair["id"]
            identity_key = "chatID" if "chatID" in arguments else "chat_id"
            arguments[identity_key] = chat_id
        if name == "search_chats" and not isinstance(arguments.get("query"), str):
            raise BeeperMCPError("search query is required")
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError") is True:
            raise BeeperMCPError("read tool returned an error")
        content = result.get("content", [])
        if not isinstance(content, list) or len(content) > self.max_items:
            raise BeeperMCPError("invalid or oversized MCP content")
        projected = []
        total_items = 0
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text" \
                    or not isinstance(block.get("text"), str) \
                    or len(block["text"]) > 20000:
                raise BeeperMCPError("invalid MCP content block")
            text = block["text"]
            if name == "search_chats":
                payload = self._parse_search_payload(text)
                rows = payload.get("chats") if isinstance(payload, dict) else payload
                if not isinstance(rows, list) or len(rows) > self.max_items:
                    raise BeeperMCPError("invalid chat search result")
                safe_rows = []
                for row in rows:
                    if not isinstance(row, dict):
                        raise BeeperMCPError("invalid chat row")
                    row_chat_id = row.get("chat_id")
                    row_chatID = row.get("chatID")
                    if row_chat_id is not None and row_chatID is not None and row_chat_id != row_chatID:
                        raise BeeperMCPError("conflicting chat identities")
                    row_id = row_chat_id if row_chat_id is not None else row_chatID
                    if row_id not in self.allowed_chats:
                        raise BeeperMCPError("search returned a disallowed chat")
                    title = row.get("title", "")
                    if not isinstance(title, str) or len(title) > 512:
                        raise BeeperMCPError("invalid chat title")
                    safe_rows.append({"chat_id": row_id, "title": title})
                total_items += len(safe_rows)
                if total_items > self.max_items:
                    raise BeeperMCPError("result exceeds item limit")
                payload = {"chats": safe_rows}
            elif name == "list_messages":
                payload = self._parse_message_payload(text, chat_id)
                top_chat_id = payload.get("chat_id") if isinstance(payload, dict) else None
                top_chatID = payload.get("chatID") if isinstance(payload, dict) else None
                if top_chat_id is not None and top_chatID is not None and top_chat_id != top_chatID:
                    raise BeeperMCPError("conflicting top-level chat identities")
                returned_chat = top_chat_id if top_chat_id is not None else top_chatID
                if returned_chat != chat_id:
                    raise BeeperMCPError("messages returned for a disallowed chat")
                rows = payload.get("messages") if isinstance(payload, dict) else None
                if not isinstance(rows, list) or len(rows) > self.max_items:
                    raise BeeperMCPError("invalid message result")
                safe_rows = []
                for row in rows:
                    if not isinstance(row, dict):
                        raise BeeperMCPError("message crossed chat boundary")
                    row_chat_id = row.get("chat_id")
                    row_chatID = row.get("chatID")
                    if row_chat_id is not None and row_chatID is not None and row_chat_id != row_chatID:
                        raise BeeperMCPError("conflicting message chat identities")
                    if (row_chat_id if row_chat_id is not None else row_chatID) not in (None, chat_id):
                        raise BeeperMCPError("message crossed chat boundary")
                    mid, body = row.get("id"), row.get("text", "")
                    if not isinstance(mid, str) or not mid or not isinstance(body, str) or len(body) > 20000:
                        raise BeeperMCPError("invalid message row")
                    safe_rows.append({"id": mid, "text": body})
                total_items += len(safe_rows)
                if total_items > self.max_items:
                    raise BeeperMCPError("result exceeds item limit")
                has_more = payload.get("has_more")
                oldest_cursor = payload.get("oldest_cursor")
                if has_more is not None and not isinstance(has_more, bool):
                    raise BeeperMCPError("invalid message pagination")
                if oldest_cursor is not None and (
                        not isinstance(oldest_cursor, str) or len(oldest_cursor) > 256):
                    raise BeeperMCPError("invalid message pagination")
                payload = {"chat_id": requested_chat_id, "messages": safe_rows}
                if has_more is not None:
                    payload["has_more"] = has_more
                if oldest_cursor is not None:
                    payload["oldest_cursor"] = oldest_cursor
            projected.append({"type": "text", "text": json.dumps(payload, separators=(",", ":"))})
        return {"content": projected}




