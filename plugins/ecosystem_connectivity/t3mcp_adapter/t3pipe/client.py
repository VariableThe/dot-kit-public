"""Minimal Streamable HTTP MCP transport for the T3 thread interface.

Speaks the MCP Streamable HTTP transport directly with ``requests`` — no MCP
SDK, no OpenCode adapter, no session-file manipulation. Handles both JSON and
SSE response bodies (the server answers either depending on content type).

The bearer token is supplied by the caller and never stored, logged, or echoed.
"""

import json

import requests

from .errors import AuthError, ConnectionError, ProtocolError, ToolError

SSE_ACCEPT = "application/json, text/event-stream"
PREFERRED_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = (PREFERRED_PROTOCOL_VERSION, "2024-11-05")
CLIENT_INFO = {"name": "t3pipe", "version": "0.1.0"}


def _parse_body(text, request_id=None):
    """Parse an MCP response body (JSON or SSE) into a JSON-RPC message.

    SSE bodies may contain multiple events (e.g. progress notifications followed
    by the actual response). We parse *every* complete event and select the one
    whose top-level ``id`` matches ``request_id``; progress/other events without
    that id are ignored instead of being returned prematurely. Malformed or
    truncated bodies raise ``ProtocolError`` rather than being silently dropped.
    """
    text = text or ""
    if not text.strip():
        return None

    messages = []
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            messages.append(json.loads(text))
        except (ValueError, TypeError) as exc:
            raise ProtocolError("could not parse MCP response body as JSON",
                                details={"error": str(exc)}) from exc
    else:
        # SSE: events are separated by a blank line; each event may contain
        # several ``data:`` lines that must be joined with newlines.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.endswith("\n\n"):
            raise ProtocolError("truncated SSE event framing")
        for event in text.split("\n\n"):
            data_lines = []
            for raw in event.splitlines():
                line = raw
                if line.startswith("data:"):
                    value = line[5:]
                    data_lines.append(value[1:] if value.startswith(" ") else value)
            if not data_lines:
                continue
            payload = "\n".join(data_lines)
            if payload == "[DONE]":
                continue
            try:
                messages.append(json.loads(payload))
            except (ValueError, TypeError) as exc:
                raise ProtocolError("could not parse SSE event as JSON",
                                    details={"error": str(exc), "payload": payload}) from exc

    if not messages:
        return None

    if request_id is not None:
        for msg in messages:
            if isinstance(msg, dict) and type(msg.get("id")) is type(request_id) and msg.get("id") == request_id:
                return msg
        # No matching id found among several events -> the real response was
        # not (cleanly) delivered. Treat as a protocol failure, not success.
        raise ProtocolError(
            "MCP response did not contain a matching JSON-RPC id",
            details={"request_id": request_id, "event_count": len(messages)},
        )

    return messages[-1] if len(messages) > 1 else messages[0]


def _extract_text(content):
    """Join ``text`` parts of a tool result's ``content`` array, then JSON-parse."""
    if not isinstance(content, list) or any(not isinstance(item, dict) for item in content):
        raise ProtocolError("tool content must be an array of objects")
    parts = [item.get("text") for item in content if item.get("type") == "text"]
    if any(not isinstance(part, str) for part in parts):
        raise ProtocolError("text content must contain strings")
    joined = "\n".join(parts)
    if not joined:
        return None
    try:
        return json.loads(joined)
    except (ValueError, TypeError):
        return joined


def _extract_error(content):
    """Pull a ``{_tag, code, message}`` error triple out of an isError content."""
    parsed = _extract_text(content)
    if isinstance(parsed, dict):
        return parsed
    return {"message": parsed if isinstance(parsed, str) else "tool error"}


class ToolResult:
    """Decoded result of one ``tools/call``."""

    def __init__(self, raw):
        self.raw = raw
        self.is_error = bool(raw.get("isError"))
        self.structured = raw.get("structuredContent")
        self.text = _extract_text(raw.get("content", []))

    @property
    def data(self):
        """Best-effort structured payload: ``structuredContent`` else parsed text."""
        if self.structured is not None:
            return self.structured
        return self.text


class T3Client:
    """A stateful MCP session to one T3 instance (one host)."""

    def __init__(self, url, token, *, timeout=30, session=None):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session_id = None
        self.protocol_version = None
        self._initialized = False

    # --- transport ---------------------------------------------------------

    def _headers(self):
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": SSE_ACCEPT,
        }
        if self.protocol_version:
            headers["mcp-protocol-version"] = self.protocol_version
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return headers

    def _post(self, payload, timeout=None):
        t = self.timeout if timeout is None else timeout
        try:
            # Redirects are refused: the policy allowlists one endpoint, and a
            # 3xx could bounce the request (and its Authorization header) to
            # a host the caller never approved. requests never re-sends
            # credentials cross-host, but we fail closed before finding out.
            resp = self.session.post(
                self.url, json=payload, headers=self._headers(), timeout=t,
                allow_redirects=False,
            )
        except requests.exceptions.Timeout as exc:
            raise ConnectionError(
                "timeout reaching T3 MCP server", details={"url": self.url}
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise ConnectionError(
                "connection refused/unreachable", details={"url": self.url}
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise ConnectionError(
                "transport error reaching T3 MCP server", details={"url": self.url, "error": str(exc)}
            ) from exc

        if resp.status_code == 401:
            raise AuthError(
                "401: credential missing, expired, revoked, or issued without "
                "--subject mcp-client",
                details={"url": self.url},
            )
        if 300 <= resp.status_code < 400:
            raise ProtocolError(
                f"HTTP {resp.status_code} redirect refused; the allowlisted "
                "endpoint must answer directly",
                details={"status": resp.status_code, "url": self.url},
            )
        if resp.status_code >= 400:
            raise ProtocolError(
                f"HTTP {resp.status_code} from MCP server",
                details={"status": resp.status_code, "url": self.url},
            )

        # The server assigns the session id on initialize; reuse it thereafter.
        if self.session_id is None:
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self.session_id = sid

        request_id = payload.get("id") if isinstance(payload, dict) else None
        parsed = _parse_body(resp.text, request_id=request_id)
        if parsed is None:
            return None
        if not isinstance(parsed, dict):
            raise ProtocolError("MCP response is not a JSON object",
                                details={"response": parsed})
        if parsed.get("jsonrpc") != "2.0":
            raise ProtocolError("invalid JSON-RPC envelope",
                                details={"jsonrpc": parsed.get("jsonrpc")})
        if request_id is not None and (("result" in parsed) == ("error" in parsed)):
            raise ProtocolError("response must contain exactly one of result or error")
        if "error" in parsed and not isinstance(parsed["error"], dict):
            raise ProtocolError("malformed JSON-RPC error",
                                details={"error": parsed.get("error")})
        return parsed

    # --- MCP lifecycle -----------------------------------------------------

    def initialize(self):
        if self._initialized:
            return self
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PREFERRED_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            }
        )
        if not isinstance(result, dict):
            raise ProtocolError("initialize response was empty or invalid",
                                details={"response": result})
        if "error" in result and result["error"] is not None:
            raise ProtocolError("initialize failed",
                                details={"error": result["error"]})
        init_result = result.get("result")
        if not isinstance(init_result, dict) or init_result.get("protocolVersion") not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ProtocolError("initialize response has missing or unsupported protocolVersion",
                                details={"result": init_result})
        self.protocol_version = init_result["protocolVersion"]
        notif_resp = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # 202/empty is expected; a structured error is not.
        if isinstance(notif_resp, dict) and notif_resp.get("error") is not None:
            raise ProtocolError("initialized notification failed",
                                details={"error": notif_resp["error"]})
        self._initialized = True
        return self

    def reconnect(self):
        """Drop the session and re-initialize. Callers must re-read state after
        a reconnect so missed events don't leave a stale view."""
        self.session_id = None
        self.protocol_version = None
        self._initialized = False
        return self.initialize()

    def list_tools(self):
        self.initialize()
        result = self._post(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        return result.get("result", {}).get("tools", [])

    # --- tool calls --------------------------------------------------------

    def call_tool(self, name, arguments=None, _id=3, timeout=None):
        self.initialize()
        payload = {
            "jsonrpc": "2.0",
            "id": _id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        result = self._post(payload, timeout=timeout)
        if result is None:
            raise ProtocolError("empty response to tools/call", details={"tool": name})
        if not isinstance(result, dict):
            raise ProtocolError("invalid tools/call response", details={"tool": name})
        if "error" in result and result["error"] is not None:
            raise ToolError(
                str(result["error"]),
                details={"error": result["error"], "tool": name},
            )
        r = result.get("result")
        if not isinstance(r, dict):
            raise ProtocolError("invalid tool result envelope", details={"tool": name})
        if r.get("isError"):
            err = _extract_error(r.get("content", []))
            raise ToolError(
                err.get("message") or "tool error",
                tag=err.get("_tag"),
                t3_code=err.get("code"),
                details=err,
            )
        return ToolResult(r)
