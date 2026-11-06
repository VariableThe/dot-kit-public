import base64
import threading

import pytest

from plugins.ecosystem_connectivity.beeper_mcp_plugin import (
    BeeperMCPPlugin, BeeperPluginError, Caller, JwtCallerVerifier, READ_SCOPE,
)
from t3mcp.auth import JwtVerifier
from helpers_t3mcp import AS, TEST_ISSUER


class Reader:
    def __init__(self, token, chats):
        self.token = token
        self.chats = chats
        self.allowed_chats = chats
        self.initialized = False
        self.discovered = False

    def initialize(self, token):
        assert token == self.token
        self.initialized = True

    def list_tools(self):
        assert self.initialized
        self.discovered = True
        return []

    def call_read_tool(self, name, arguments):
        assert self.discovered
        if name == "search_chats":
            return {"content": [{"type": "text", "text": '{"chats":[]}'}]}
        return {"content": [{"type": "text", "text":
                              '{"chat_id":"allowed","messages":[]}'}]}


def make_plugin(verify=None, provider=None):
    caller = Caller("subject-1", frozenset({READ_SCOPE}), frozenset({"allowed"}))
    verify = verify or (lambda token: caller)
    provider = provider or (lambda identity: "beeper-runtime-token")
    return BeeperMCPPlugin(
        verify_caller=verify,
        beeper_token_provider=provider,
        reader_factory=lambda token, chats: Reader(token, chats),
        server_resource="https://dot.example/mcp",
    )


def test_constructor_requires_external_auth_and_separate_provider():
    with pytest.raises(BeeperPluginError):
        BeeperMCPPlugin(verify_caller=None, beeper_token_provider=None,
                        reader_factory=None, server_resource="https://dot.example/mcp")
    with pytest.raises(BeeperPluginError):
        BeeperMCPPlugin(verify_caller=lambda token: Caller("s", frozenset({READ_SCOPE}), frozenset({"c"})),
                        beeper_token_provider=lambda caller: "token",
                        reader_factory=lambda token, chats: Reader(token, chats),
                        server_resource="http://localhost.evil/mcp")


def test_initialize_and_tools_list_are_bounded_and_authenticated():
    plugin = make_plugin()
    init = plugin.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                         "Bearer caller-token")
    assert init.status == 200
    listed = plugin.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                           "Bearer caller-token")
    names = {tool["name"] for tool in listed.body["result"]["tools"]}
    assert names == {"search_chats", "list_messages"}


def test_invalid_auth_is_401_and_caller_token_is_not_forwarded():
    seen = []
    plugin = make_plugin(verify=lambda token: (_ for _ in ()).throw(ValueError()))
    response = plugin.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                             "Bearer caller-token")
    assert response.status == 401
    assert "caller-token" not in str(response.body)


def test_malformed_verified_identity_is_forbidden():
    plugin = make_plugin(verify=lambda token: Caller("subject", frozenset(), frozenset({"allowed"})))
    response = plugin.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                             "Bearer caller-token")
    assert response.status == 403


def test_read_uses_separate_beeper_token_and_per_caller_chat_policy():
    calls = []
    plugin = make_plugin(provider=lambda caller: calls.append(caller.subject) or "beeper-token")
    response = plugin.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "list_messages",
                                         "arguments": {"chatID": "allowed"}}},
                             "Bearer caller-token")
    assert response.status == 200
    assert response.body["result"]["content"]
    assert calls == ["subject-1"]
    assert "caller-token" not in str(response.body)


def test_disallowed_chat_is_403_before_reader_creation():
    created = []
    caller = Caller("subject-1", frozenset({READ_SCOPE}), frozenset({"other"}))
    plugin = BeeperMCPPlugin(
        verify_caller=lambda token: caller,
        beeper_token_provider=lambda identity: "beeper-token",
        reader_factory=lambda token, chats: created.append(1),
        server_resource="https://dot.example/mcp")
    response = plugin.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "list_messages",
                                         "arguments": {"chatID": "allowed"}}},
                             "Bearer caller-token")
    assert response.status == 403
    assert created == []


def test_mutations_unknown_methods_and_bad_requests_fail_closed():
    plugin = make_plugin()
    mutation = plugin.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "send_message", "arguments": {}}},
                             "Bearer caller-token")
    assert mutation.body["error"]["code"] == -32003
    unknown = plugin.handle({"jsonrpc": "2.0", "id": 2, "method": "send_message"},
                            "Bearer caller-token")
    assert unknown.body["error"]["code"] == -32601
    bad = plugin.handle({"id": 3, "method": "tools/list"}, "Bearer caller-token")
    assert bad.body["error"]["code"] == -32602
    bad_id = plugin.handle({"jsonrpc": "2.0", "id": True, "method": "tools/list"},
                           "Bearer caller-token")
    assert bad_id.body["error"]["code"] == -32602
    notification = plugin.handle({"jsonrpc": "2.0", "method": "notifications/initialized"},
                                 "Bearer caller-token")
    assert notification.body is None and notification.status == 202


def test_jwt_verifier_binds_expiry_audience_scope_and_user_chat_entitlements():
    resource = "https://dot.example/mcp"
    verifier = JwtVerifier(issuer=TEST_ISSUER, audience=resource,
                           algorithm="RS256", jwks=AS.jwks(), leeway_s=0)
    caller_verifier = JwtCallerVerifier(verifier, {"alice": {"allowed"}, "bob": {"other"}},
                                        resource=resource)
    plugin = BeeperMCPPlugin(
        verify_caller=caller_verifier,
        beeper_token_provider=lambda caller: "beeper-only-token",
        reader_factory=lambda token, chats: Reader(token, chats),
        server_resource=resource)

    alice = AS.token(subject="alice", scopes=(READ_SCOPE,), audience=resource)
    ok = plugin.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "list_messages",
                                   "arguments": {"chatID": "allowed"}}},
                       "Bearer " + alice)
    assert ok.status == 200
    assert '"chat_id":"allowed"' in ok.body["result"]["content"][0]["text"]
    expired = AS.token(subject="alice", scopes=(READ_SCOPE,), expires_in=-10, audience=resource)
    assert plugin.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                         "Bearer " + expired).status == 401
    wrong_audience = AS.token(subject="alice", scopes=(READ_SCOPE,), audience="wrong")
    assert plugin.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                         "Bearer " + wrong_audience).status == 401
    bob = AS.token(subject="bob", scopes=(READ_SCOPE,), audience=resource)
    denied = plugin.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "list_messages",
                                       "arguments": {"chatID": "allowed"}}},
                           "Bearer " + bob)
    assert denied.status == 403


def test_jwt_rejects_wrong_issuer_bad_signature_and_missing_scope():
    resource = "https://dot.example/mcp"
    verifier = JwtVerifier(issuer=TEST_ISSUER, audience=resource,
                           algorithm="RS256", jwks=AS.jwks(), leeway_s=0)
    caller_verifier = JwtCallerVerifier(verifier, {"alice": {"allowed"}}, resource=resource)
    plugin = BeeperMCPPlugin(verify_caller=caller_verifier,
                             beeper_token_provider=lambda caller: "beeper-only-token",
                             reader_factory=lambda token, chats: Reader(token, chats),
                             server_resource=resource)
    wrong_issuer = AS.token(subject="alice", scopes=(READ_SCOPE,), audience=resource,
                            issuer="https://wrong.example")
    assert plugin.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         "Bearer " + wrong_issuer).status == 401
    valid = AS.token(subject="alice", scopes=(READ_SCOPE,), audience=resource)
    header, payload, signature = valid.split(".")
    signature_bytes = bytearray(base64.urlsafe_b64decode(signature + "=="))
    signature_bytes[0] ^= 1
    bad_signature = header + "." + payload + "." + base64.urlsafe_b64encode(
        bytes(signature_bytes)).rstrip(b"=").decode("ascii")
    assert plugin.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                         "Bearer " + bad_signature).status == 401
    missing_scope = AS.token(subject="alice", scopes=(), audience=resource)
    assert plugin.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                         "Bearer " + missing_scope).status == 403


def test_two_users_keep_entitlements_and_content_isolation():
    resource = "https://dot.example/mcp"
    verifier = JwtVerifier(issuer=TEST_ISSUER, audience=resource,
                           algorithm="RS256", jwks=AS.jwks(), leeway_s=0)
    caller_verifier = JwtCallerVerifier(verifier,
                                        {"alice": {"alice-chat"}, "bob": {"bob-chat"}},
                                        resource=resource)

    class IdentityReader(Reader):
        def call_read_tool(self, name, arguments):
            chat = next(iter(self.allowed_chats))
            return {"content": [{"type": "text", "text":
                                  '{"chat_id":"' + chat + '","messages":[]}'}]}

    seen = []
    barrier = threading.Barrier(2)
    plugin = BeeperMCPPlugin(
        verify_caller=caller_verifier,
        beeper_token_provider=lambda caller: (barrier.wait(),
                                              seen.append(caller.subject),
                                              "backend-token")[-1],
        reader_factory=lambda token, chats: IdentityReader(token, chats),
        server_resource=resource)

    results = {}
    def invoke(index, subject, chat):
        token = AS.token(subject=subject, scopes=(READ_SCOPE,), audience=resource)
        results[subject] = plugin.handle(
            {"jsonrpc": "2.0", "id": index, "method": "tools/call",
             "params": {"name": "list_messages", "arguments": {"chatID": chat}}},
            "Bearer " + token)

    threads = [threading.Thread(target=invoke, args=(1, "alice", "alice-chat")),
               threading.Thread(target=invoke, args=(2, "bob", "bob-chat"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    for subject, chat in (("alice", "alice-chat"), ("bob", "bob-chat")):
        response = results[subject]
        assert response.status == 200
        assert chat in response.body["result"]["content"][0]["text"]
    assert set(seen) == {"alice", "bob"}


def test_composed_real_readonly_client_never_receives_caller_token():
    resource = "https://dot.example/mcp"
    verifier = JwtVerifier(issuer=TEST_ISSUER, audience=resource,
                           algorithm="RS256", jwks=AS.jwks(), leeway_s=0)
    caller_verifier = JwtCallerVerifier(verifier,
                                        {"alice": {"alice-chat"}, "bob": {"bob-chat"}},
                                        resource=resource)
    transports = []

    class MCPTransport:
        def __init__(self, chats):
            self.chats = chats
            self.protocol_version = None
            self.session_id = None
            self.tokens = []
            transports.append(self)

        def post(self, payload, token):
            self.tokens.append(token)
            method = payload["method"]
            if method == "initialize":
                return {"jsonrpc": "2.0", "id": payload["id"],
                        "result": {"protocolVersion": "2024-11-05"}}
            if method == "notifications/initialized":
                return {"jsonrpc": "2.0", "result": {}}
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": [{
                    "name": "list_messages", "inputSchema": {
                        "type": "object", "properties": {"chatID": {"type": "string"}},
                        "required": ["chatID"]}}]}}
            chat = payload["params"]["arguments"]["chatID"]
            assert chat in self.chats
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {
                "content": [{"type": "text", "text":
                    '{"chatID":"' + chat + '","messages":[]}'}]}}

    def factory(token, chats):
        from plugins.ecosystem_connectivity.beeper_mcp_readonly import ReadOnlyBeeperMCP
        return ReadOnlyBeeperMCP(MCPTransport(chats), read_tool_allowlist={"list_messages"},
                                 allowed_chats=chats)

    plugin = BeeperMCPPlugin(
        verify_caller=caller_verifier,
        beeper_token_provider=lambda caller: "backend-for-" + caller.subject,
        reader_factory=factory,
        server_resource=resource)
    for index, subject, chat in ((10, "alice", "alice-chat"), (11, "bob", "bob-chat")):
        token = AS.token(subject=subject, scopes=(READ_SCOPE,), audience=resource)
        response = plugin.handle(
            {"jsonrpc": "2.0", "id": index, "method": "tools/call",
             "params": {"name": "list_messages", "arguments": {"chatID": chat}}},
            "Bearer " + token)
        assert response.status == 200
        assert chat in response.body["result"]["content"][0]["text"]
    assert [transport.tokens[0] for transport in transports] == ["backend-for-alice", "backend-for-bob"]
    assert all("Bearer " not in token for transport in transports for token in transport.tokens)


def test_missing_beeper_provider_result_returns_backend_error():
    plugin = make_plugin(provider=lambda caller: None)
    response = plugin.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "search_chats",
                                         "arguments": {"query": "x"}}},
                             "Bearer caller-token")
    assert response.body["error"]["code"] == -32000

