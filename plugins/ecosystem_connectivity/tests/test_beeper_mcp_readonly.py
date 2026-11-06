import json

import pytest

from plugins.ecosystem_connectivity.beeper_mcp_readonly import (
    BeeperMCPError, ReadOnlyBeeperMCP, StreamableHTTPTransport,
)

SEARCH_SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}},
                 "required": ["query"]}
LIST_SCHEMA = {"type": "object", "properties": {"chatID": {"type": "string"}},
               "required": ["chatID"]}


class FakeTransport:
    def __init__(self):
        self.calls = []

    def post(self, payload, token):
        self.calls.append((payload, token))
        method = payload["method"]
        if method == "notifications/initialized":
            return {"jsonrpc": "2.0", "result": {}}
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": payload["id"],
                    "result": {"protocolVersion": "2024-11-05"}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": payload["id"],
                    "result": {"tools": [
                        {"name": "search_chats", "inputSchema": SEARCH_SCHEMA},
                        {"name": "list_messages", "inputSchema": LIST_SCHEMA},
                    ]}}
        return {"jsonrpc": "2.0", "id": payload["id"],
                "result": {"content": [{"type": "text", "text": "[]"}]}}


def test_readonly_protocol_and_runtime_token():
    fake = FakeTransport()
    client = ReadOnlyBeeperMCP(fake)
    assert client.initialize("runtime-token")["protocolVersion"] == "2024-11-05"
    assert {tool["name"] for tool in client.list_tools()} == {"search_chats", "list_messages"}
    result = client.call_read_tool("search_chats", {"query": "x"})
    assert result["content"][0]["text"] == "{\"chats\":[]}" 
    assert all(token == "runtime-token" for _, token in fake.calls)


def test_mutations_and_bad_lifecycle_fail_closed():
    client = ReadOnlyBeeperMCP(FakeTransport())
    with pytest.raises(BeeperMCPError):
        client.list_tools()
    client.initialize("runtime-token")
    client.list_tools()
    with pytest.raises(BeeperMCPError):
        client.call_read_tool("send_message", {})
    before = len(client.transport.calls)
    with pytest.raises(BeeperMCPError, match="tool arguments"):
        client.call_read_tool("search_chats", [])
    assert len(client.transport.calls) == before
    with pytest.raises(BeeperMCPError):
        client.call_read_tool("search_chats_extra", {})


def test_sse_matches_jsonrpc_id_and_transport_rejects_redirects():
    sse = b"event: message\r\nid: 1\r\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}\r\n\r\n" \
          b"event: message\r\nid: 2\r\ndata: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"ok\":true}}\r\n\r\n"
    assert StreamableHTTPTransport._decode(sse, expected_id=2)["id"] == 2
    with pytest.raises(BeeperMCPError):
        StreamableHTTPTransport._decode(sse, expected_id=3)
    unrelated = b"event: message\nid: 99\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"note\"}\n\n" \
        b"event: message\nid: 2\ndata: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{}}\n\n"
    assert StreamableHTTPTransport._decode(unrelated, expected_id=2)["id"] == 2

    class Response:
        status = 302
        headers = {"Location": "http://evil.example"}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit): return b""

    seen = []
    def opener(request, timeout):
        seen.append(request)
        return Response()

    transport = StreamableHTTPTransport("http://localhost:23373/v0/mcp", opener=opener)
    with pytest.raises(BeeperMCPError):
        transport.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "runtime")
    assert seen[0].get_header("Authorization") == "Bearer runtime"


def test_mcp_rejects_alias_conflicts_and_aggregate_overflow():
    class Adversarial(FakeTransport):
        def post(self, payload, token):
            if payload["method"] == "notifications/initialized":
                return {"jsonrpc": "2.0", "result": {}}
            if payload["method"] == "initialize":
                return {"jsonrpc": "2.0", "id": payload["id"],
                        "result": {"protocolVersion": "2024-11-05"}}
            if payload["method"] == "tools/list":
                return {"jsonrpc": "2.0", "id": payload["id"],
                        "result": {"tools": [{"name": "list_messages", "inputSchema": LIST_SCHEMA}]}}
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {
                "content": [{"type": "text", "text":
                    '{"chat_id":"allowed","chatID":"secret","messages":[]}'}]}}

    client = ReadOnlyBeeperMCP(Adversarial(), read_tool_allowlist={"list_messages"},
                               allowed_chats={"allowed"})
    client.initialize("runtime-token")
    client.list_tools()
    with pytest.raises(BeeperMCPError):
        client.call_read_tool("list_messages", {"chat_id": "allowed"})


def test_mcp_rejects_per_message_alias_and_search_conflicts():
    class Rows(FakeTransport):
        def __init__(self, text):
            super().__init__(); self.text = text
        def post(self, payload, token):
            if payload["method"] == "notifications/initialized":
                return {"jsonrpc": "2.0", "result": {}}
            if payload["method"] == "initialize":
                return {"jsonrpc": "2.0", "id": payload["id"],
                        "result": {"protocolVersion": "2024-11-05"}}
            if payload["method"] == "tools/list":
                return {"jsonrpc": "2.0", "id": payload["id"],
                        "result": {"tools": [
                            {"name": "list_messages", "inputSchema": LIST_SCHEMA},
                            {"name": "search_chats", "inputSchema": SEARCH_SCHEMA},
                        ]}}
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {
                "content": [{"type": "text", "text": self.text}]}}

    per_message = Rows('{"chat_id":"allowed","messages":[{"id":"m","chatID":"secret","text":"x"}]}')
    client = ReadOnlyBeeperMCP(per_message, allowed_chats={"allowed"})
    client.initialize("runtime-token")
    client.list_tools()
    with pytest.raises(BeeperMCPError):
        client.call_read_tool("list_messages", {"chat_id": "allowed"})

    search_conflict = Rows('{"chats":[{"chat_id":"allowed","chatID":"secret","title":"x"}]}')
    client = ReadOnlyBeeperMCP(search_conflict, allowed_chats={"allowed"})
    client.initialize("runtime-token")
    client.list_tools()
    with pytest.raises(BeeperMCPError):
        client.call_read_tool("search_chats", {"query": "x"})


def test_mcp_aggregate_bound_spans_multiple_content_blocks():
    class Many(FakeTransport):
        def post(self, payload, token):
            if payload["method"] == "notifications/initialized":
                return {"jsonrpc": "2.0", "result": {}}
            if payload["method"] == "initialize":
                return {"jsonrpc": "2.0", "id": payload["id"],
                        "result": {"protocolVersion": "2024-11-05"}}
            if payload["method"] == "tools/list":
                return {"jsonrpc": "2.0", "id": payload["id"],
                        "result": {"tools": [
                            {"name": "search_chats", "inputSchema": SEARCH_SCHEMA},
                            {"name": "list_messages", "inputSchema": LIST_SCHEMA},
                        ]}}
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {
                "content": [
                    {"type": "text", "text": '{"chats":[{"chat_id":"allowed","title":"a"},{"chat_id":"allowed","title":"b"}]}'},
                    {"type": "text", "text": '{"chats":[{"chat_id":"allowed","title":"c"},{"chat_id":"allowed","title":"d"}]}'},
                ]}}

    client = ReadOnlyBeeperMCP(Many(), allowed_chats={"allowed"}, max_items=2)
    client.initialize("runtime-token")
    client.list_tools()
    with pytest.raises(BeeperMCPError):
        client.call_read_tool("search_chats", {"query": "x"})


def test_default_opener_path_session_and_protocol_headers_and_byte_limit():
    class Response:
        status = 200
        headers = {"Mcp-Session-Id": "session-1"}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit): return b'{"jsonrpc":"2.0","id":1,"result":{}}'
    class Opener:
        def __init__(self): self.requests = []
        def open(self, request, timeout): self.requests.append(request); return Response()
    opener = Opener()
    transport = StreamableHTTPTransport("http://localhost:23373/v0/mcp", opener=opener)
    transport.protocol_version = "2025-03-26"
    transport.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "runtime")
    transport.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "runtime")
    headers = {key.lower(): value for key, value in opener.requests[1].headers.items()}
    assert headers["mcp-session-id"] == "session-1"
    assert headers["mcp-protocol-version"] == "2025-03-26"

    class Huge(Response):
        def read(self, limit): return b"x" * (limit + 1)
    class HugeOpener:
        def open(self, request, timeout): return Huge()
    bounded = StreamableHTTPTransport("http://localhost:23373/v0/mcp",
                                      max_response_bytes=10, opener=HugeOpener())
    with pytest.raises(BeeperMCPError):
        bounded.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "runtime")


def test_transport_auth_protocol_and_response_limits():
    transport = StreamableHTTPTransport("http://localhost:23373/v0/mcp", max_response_bytes=10)
    with pytest.raises(BeeperMCPError):
        transport.post({"jsonrpc": "2.0"}, "")
    with pytest.raises(BeeperMCPError):
        transport._decode(b"not-json")
    with pytest.raises(BeeperMCPError):
        transport._decode(json.dumps({"result": {}}).encode() + b"x" * 20)


def test_observed_markdown_search_cards_are_projected_and_allowlisted():
    class Cards(FakeTransport):
        def post(self, payload, token):
            if payload["method"] == "tools/list":
                return {"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": [{
                    "name": "search_chats", "inputSchema": SEARCH_SCHEMA}]}}
            if payload["method"] == "tools/call":
                return {"jsonrpc": "2.0", "id": payload["id"], "result": {
                    "content": [{"type": "text", "text":
                        "## Alice (chatID: allowed)\nChat on Signal (signal) with Alice.\n# Messages\n## Secret (chatID: secret)\nNot a chat card."}]}}
            return super().post(payload, token)

    client = ReadOnlyBeeperMCP(Cards(), read_tool_allowlist={"search_chats"},
                               allowed_chats={"allowed"})
    client.initialize("runtime-token")
    client.list_tools()
    result = client.call_read_tool("search_chats", {"query": "Alice"})
    assert '"chat_id":"allowed"' in result["content"][0]["text"]


def test_observed_message_items_map_pagination_and_reject_cross_chat_rows():
    schema = {"type": "object", "properties": {
        "chatID": {"type": "string"}, "cursor": {"type": "string"},
        "direction": {"type": "string"}}, "required": ["chatID"]}

    class Pages(FakeTransport):
        def post(self, payload, token):
            if payload["method"] == "tools/list":
                return {"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": [{
                    "name": "list_messages", "inputSchema": schema}]}}
            if payload["method"] == "tools/call":
                args = payload["params"]["arguments"]
                assert args == {"chatID": "allowed", "cursor": "old", "direction": "before"}
                return {"jsonrpc": "2.0", "id": payload["id"], "result": {
                    "content": [{"type": "text", "text": json.dumps({
                        "chatID": "allowed",
                        "items": [{"id": "m1", "chatID": "allowed", "text": "hello"}],
                        "hasMore": True, "oldestCursor": "older"})}]}}
            return super().post(payload, token)

    client = ReadOnlyBeeperMCP(Pages(), read_tool_allowlist={"list_messages"},
                               allowed_chats={"allowed"})
    client.initialize("runtime-token")
    client.list_tools()
    result = client.call_read_tool("list_messages", {
        "chatID": "allowed", "cursor": "old", "direction": "before"})
    text = result["content"][0]["text"]
    assert '"has_more":true' in text and '"oldest_cursor":"older"' in text
    assert '"id":"m1"' in text


def test_observed_message_results_accept_proven_item_identity_and_reject_cross_chat_rows():
    schema = {"type": "object", "properties": {"chatID": {"type": "string"}},
              "required": ["chatID"]}

    class Pages(FakeTransport):
        def __init__(self, result):
            super().__init__()
            self.result = result

        def post(self, payload, token):
            if payload["method"] == "tools/list":
                return {"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": [{
                    "name": "list_messages", "inputSchema": schema}]}}
            if payload["method"] == "tools/call":
                return {"jsonrpc": "2.0", "id": payload["id"], "result": {
                    "content": [{"type": "text", "text": json.dumps(self.result)}]}}
            return super().post(payload, token)

    identity_free = Pages({
        "items": [{"id": "m1", "chatID": "allowed", "text": "hello"}],
        "hasMore": False, "oldestCursor": None,
    })
    client = ReadOnlyBeeperMCP(identity_free, read_tool_allowlist={"list_messages"},
                               allowed_chats={"allowed"})
    client.initialize("runtime-token")
    client.list_tools()
    result = client.call_read_tool("list_messages", {"chatID": "allowed"})
    assert json.loads(result["content"][0]["text"])["chat_id"] == "allowed"

    cross_chat = Pages({
        "chatID": "allowed",
        "items": [{"id": "m1", "chatID": "secret", "text": "secret"}],
        "hasMore": False, "oldestCursor": None,
    })
    client = ReadOnlyBeeperMCP(cross_chat, read_tool_allowlist={"list_messages"},
                               allowed_chats={"allowed"})
    client.initialize("runtime-token")
    client.list_tools()
    with pytest.raises(BeeperMCPError, match="crossed chat"):
        client.call_read_tool("list_messages", {"chatID": "allowed"})


def test_legacy_message_pagination_is_bounded_and_typed():
    class Legacy(FakeTransport):
        def post(self, payload, token):
            if payload["method"] == "tools/call":
                return {"jsonrpc": "2.0", "id": payload["id"], "result": {
                    "content": [{"type": "text", "text":
                        '{"chat_id":"allowed","messages":[],"has_more":"yes"}'}]}}
            return super().post(payload, token)

    client = ReadOnlyBeeperMCP(Legacy(), read_tool_allowlist={"list_messages"},
                               allowed_chats={"allowed"})
    client.initialize("runtime-token")
    client.list_tools()
    with pytest.raises(BeeperMCPError, match="pagination"):
        client.call_read_tool("list_messages", {"chatID": "allowed"})


def test_observed_malformed_markdown_and_protocol_version_fail_closed():
    class Bad(FakeTransport):
        def post(self, payload, token):
            if payload["method"] == "initialize":
                return {"jsonrpc": "2.0", "id": payload["id"],
                        "result": {"protocolVersion": "2025-06-18"}}
            if payload["method"] == "tools/list":
                return {"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": [{
                    "name": "search_chats", "inputSchema": SEARCH_SCHEMA}]}}
            if payload["method"] == "tools/call":
                return {"jsonrpc": "2.0", "id": payload["id"], "result": {
                    "content": [{"type": "text", "text": "## Broken card"}]}}
            return super().post(payload, token)

    client = ReadOnlyBeeperMCP(Bad(), read_tool_allowlist={"search_chats"},
                               allowed_chats={"allowed"})
    assert client.initialize("runtime-token")["protocolVersion"] == "2025-06-18"
    client.list_tools()
    with pytest.raises(BeeperMCPError, match="malformed"):
        client.call_read_tool("search_chats", {"query": "x"})


def test_failed_rediscovery_clears_previous_schema():
    class Flaky(FakeTransport):
        def __init__(self):
            super().__init__(); self.fail = False
        def post(self, payload, token):
            if payload["method"] == "tools/list" and self.fail:
                return {"jsonrpc": "2.0", "id": payload["id"],
                        "result": {"tools": [{"name": "search_chats", "inputSchema": {}}]}}
            return super().post(payload, token)

    transport = Flaky()
    client = ReadOnlyBeeperMCP(transport, read_tool_allowlist={"search_chats"})
    client.initialize("runtime-token")
    client.list_tools()
    transport.fail = True
    with pytest.raises(BeeperMCPError, match="incompatible"):
        client.list_tools()
    before = len(transport.calls)
    with pytest.raises(BeeperMCPError, match="discovery"):
        client.call_read_tool("search_chats", {"query": "x"})
    assert len(transport.calls) == before


def test_failed_reinitialize_clears_token_session_and_schema():
    class Flaky(FakeTransport):
        def __init__(self):
            super().__init__(); self.fail = False
        def post(self, payload, token):
            if payload["method"] == "initialize" and self.fail:
                return {"jsonrpc": "2.0", "id": payload["id"],
                        "result": {"protocolVersion": "unsupported"}}
            return super().post(payload, token)

    transport = Flaky()
    client = ReadOnlyBeeperMCP(transport, read_tool_allowlist={"search_chats"})
    client.initialize("old-token")
    client.list_tools()
    transport.session_id = "old-session"
    transport.fail = True
    with pytest.raises(BeeperMCPError, match="protocol version"):
        client.initialize("new-token")
    assert client._initialized is False
    assert client._schemas is None
    assert client.transport_token is None
    assert transport.session_id is None
    with pytest.raises(BeeperMCPError, match="initialized"):
        client.call_read_tool("search_chats", {"query": "x"})


def test_schema_rejects_unknown_required_field():
    class BadSchema(FakeTransport):
        def post(self, payload, token):
            if payload["method"] == "tools/list":
                return {"jsonrpc": "2.0", "id": payload["id"],
                        "result": {"tools": [{"name": "search_chats",
                            "inputSchema": {"type": "object",
                                "properties": {"query": {"type": "string"},
                                                "tenant": {"type": "string"}},
                                "required": ["query", "tenant"]}}]}}
            return super().post(payload, token)

    client = ReadOnlyBeeperMCP(BadSchema(), read_tool_allowlist={"search_chats"})
    client.initialize("runtime-token")
    with pytest.raises(BeeperMCPError, match="incompatible"):
        client.list_tools()


def test_arguments_must_match_discovered_schema_before_call():
    transport = FakeTransport()
    client = ReadOnlyBeeperMCP(transport, read_tool_allowlist={"search_chats"})
    client.initialize("runtime-token")
    client.list_tools()
    before = len(transport.calls)
    with pytest.raises(BeeperMCPError, match="not supported"):
        client.call_read_tool("search_chats", {"query": "x", "scope": "all"})
    assert len(transport.calls) == before


@pytest.mark.parametrize("schema_key", ["chatID", "chat_id"])
@pytest.mark.parametrize("input_key", ["chatID", "chat_id"])
def test_list_messages_normalizes_each_supported_chat_alias(schema_key, input_key):
    schema = {"type": "object", "properties": {schema_key: {"type": "string"}},
              "required": [schema_key]}

    class AliasTransport(FakeTransport):
        def post(self, payload, token):
            if payload["method"] == "tools/list":
                return {"jsonrpc": "2.0", "id": payload["id"],
                        "result": {"tools": [{"name": "list_messages",
                            "inputSchema": schema}]}}
            if payload["method"] == "tools/call":
                args = payload["params"]["arguments"]
                assert args == {schema_key: "allowed"}
                return {"jsonrpc": "2.0", "id": payload["id"], "result": {
                    "content": [{"type": "text", "text":
                        '{"chat_id":"allowed","messages":[]}'}]}}
            return super().post(payload, token)

    transport = AliasTransport()
    client = ReadOnlyBeeperMCP(transport, read_tool_allowlist={"list_messages"},
                               allowed_chats={"allowed"})
    client.initialize("runtime-token")
    client.list_tools()
    client.call_read_tool("list_messages", {input_key: "allowed"})



