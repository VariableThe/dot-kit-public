"""Offline provider-identity binding tests, using synthetic data only."""
import json
import pytest
from plugins.ecosystem_connectivity.beeper_mcp_readonly import (
    BeeperMCPError, ReadOnlyBeeperMCP, StreamableHTTPTransport, _verified_identity_pair)

PAIR = {'id': 'canonical-A', 'localChatID': '123'}
class Response:
    status = 200
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, limit): return self.body[:limit]

def test_transport_same_origin_exact_encoding_and_minimal_projection():
    seen = []
    metadata = {**PAIR, 'title': 'not retained', 'participants': {'items': []}, 'draft': {'text': 'not retained'}}
    def opener(request, timeout):
        seen.append(request)
        assert timeout == 20
        return Response(json.dumps(metadata).encode())
    transport = StreamableHTTPTransport('http://127.0.0.1:23373/v0/mcp', opener=opener)
    assert transport.resolve_chat_identity('123', 'synthetic-token') == PAIR
    assert seen[0].full_url == 'http://127.0.0.1:23373/v1/chats/123?maxParticipantCount=0'
    assert seen[0].method == 'GET'
    assert seen[0].get_header('Authorization') == 'Bearer synthetic-token'
    assert seen[0].get_header('Mcp-session-id') is None

@pytest.mark.parametrize('change', [
    {'id': None}, {'id': 4}, {'id': ''}, {'localChatID': None}, {'localChatID': 123},
    {'localChatID': '124'}, {'chatID': 'other'}, {'chat_id': None},
    {'merge': {}}, {'merge': {'chatIDs': ['other']}}, {'mergedIntoChatID': 'other'},
])
def test_invalid_mapping_fails_closed(change):
    with pytest.raises(BeeperMCPError): _verified_identity_pair({**PAIR, **change}, '123')

@pytest.mark.parametrize('body', [b'[]', b'null', b'not-json', b'{"id":"canonical-A","id":"canonical-B","localChatID":"123"}'])
def test_malformed_or_duplicate_json_rejected(body):
    transport = StreamableHTTPTransport('http://localhost:23373/v0/mcp', opener=lambda *a, **k: Response(body))
    with pytest.raises(BeeperMCPError): transport.resolve_chat_identity('123', 'synthetic')

@pytest.mark.parametrize('status', [301, 302, 307, 308, 401, 403, 500])
def test_non_success_rejected(status):
    response = Response(json.dumps(PAIR).encode()); response.status = status
    transport = StreamableHTTPTransport('http://localhost:23373/v0/mcp', opener=lambda *a, **k: response)
    with pytest.raises(BeeperMCPError): transport.resolve_chat_identity('123', 'synthetic')

def test_oversized_metadata_rejected():
    transport = StreamableHTTPTransport('http://localhost:23373/v0/mcp', max_response_bytes=5, opener=lambda *a, **k: Response(b'x'*6))
    with pytest.raises(BeeperMCPError): transport.resolve_chat_identity('123', 'synthetic')

class Fake:
    def __init__(self, canonical='canonical-A'):
        self.canonical = canonical; self.calls = []; self.lookups = []
    def resolve_chat_identity(self, request, token):
        self.lookups.append((request, token))
        return {'id': self.canonical, 'localChatID': '123'}
    def post(self, payload, token):
        args = payload['params']['arguments']; self.calls.append(args)
        assert args['chatID'] == self.canonical
        result = {'items': [{'id': 'message', 'chatID': self.canonical, 'text': 'synthetic'}], 'hasMore': False, 'oldestCursor': None}
        return {'id': payload['id'], 'result': {'content': [{'type': 'text', 'text': json.dumps(result)}]}}

def client(fake, token='synthetic', allowed=frozenset({'123'})):
    c = ReadOnlyBeeperMCP(fake, allowed_chats=allowed)
    c._initialized = True; c.transport_token = token
    c._schemas = {'list_messages': {'properties': {'chatID': {'type': 'string'}, 'cursor': {'type': 'string'}}, 'required': ['chatID']}}
    return c

def test_request_local_mapping_and_projection():
    fake = Fake(); c = client(fake)
    result = c.call_read_tool('list_messages', {'chatID': '123'})
    assert json.loads(result['content'][0]['text'])['chat_id'] == '123'
    assert c.allowed_chats == frozenset({'123'})
    c.call_read_tool('list_messages', {'chatID': '123', 'cursor': 'opaque'})
    assert len(fake.lookups) == 2
    assert fake.calls[-1] == {'chatID': 'canonical-A', 'cursor': 'opaque'}

def test_unentitled_request_cannot_trigger_metadata_read():
    fake = Fake(); c = client(fake)
    with pytest.raises(BeeperMCPError): c.call_read_tool('list_messages', {'chatID': 'canonical-A'})
    assert fake.lookups == [] and fake.calls == []

def test_two_instances_and_callers_do_not_share_mapping():
    a, b = Fake('canonical-A'), Fake('canonical-B')
    client(a, 'user-A').call_read_tool('list_messages', {'chatID': '123'})
    client(b, 'user-B').call_read_tool('list_messages', {'chatID': '123'})
    assert a.lookups == [('123', 'user-A')] and b.lookups == [('123', 'user-B')]
    assert a.calls[0]['chatID'] != b.calls[0]['chatID']

def test_bad_mapping_prevents_message_call():
    fake = Fake(); fake.resolve_chat_identity = lambda *a: {'id': 'other', 'localChatID': 'wrong'}
    with pytest.raises(BeeperMCPError): client(fake).call_read_tool('list_messages', {'chatID': '123'})
    assert fake.calls == []

def test_metadata_failure_is_not_bypassed():
    fake = Fake()
    def fail(*a): raise BeeperMCPError('lookup failed')
    fake.resolve_chat_identity = fail
    with pytest.raises(BeeperMCPError): client(fake).call_read_tool('list_messages', {'chatID': '123'})
    assert fake.calls == []

def test_path_injection_is_encoded_and_origin_preserved():
    requested = '../x?y#z'; seen=[]
    def opener(req, **kw):
        seen.append(req.full_url)
        return Response(json.dumps({'id': 'canonical', 'localChatID': requested}).encode())
    transport = StreamableHTTPTransport('http://[::1]:23373/v0/mcp', opener=opener)
    transport.resolve_chat_identity(requested, 'synthetic')
    assert seen == ['http://[::1]:23373/v1/chats/..%2Fx%3Fy%23z?maxParticipantCount=0']

@pytest.mark.parametrize('row', [
    {'id': 'm', 'chatID': 'canonical-B'}, {'id': 'm'},
    {'id': 'm', 'chatID': 'canonical-A', 'chat_id': 'canonical-B'},
])
def test_resolved_identity_does_not_weaken_row_checks(row):
    fake = Fake()
    def post(payload, token):
        result = {'items': [row], 'hasMore': False, 'oldestCursor': None}
        return {'id': payload['id'], 'result': {'content': [{'type': 'text', 'text': json.dumps(result)}]}}
    fake.post = post
    with pytest.raises(BeeperMCPError): client(fake).call_read_tool('list_messages', {'chatID': '123'})

def test_default_opener_rejects_redirect_and_disables_ambient_proxy():
    import urllib.request
    transport = StreamableHTTPTransport('http://localhost:23373/v0/mcp')
    redirects = [h for h in transport.opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert len(redirects) == 1
    assert redirects[0].redirect_request(None, None, 302, '', {}, 'https://other.invalid') is None
    assert not any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in transport.opener.handlers)

def test_errors_do_not_expose_metadata_or_identity():
    def fail(*a, **k): raise OSError('sensitive synthetic identifier')
    transport = StreamableHTTPTransport('http://localhost:23373/v0/mcp', opener=fail)
    with pytest.raises(BeeperMCPError) as err: transport.resolve_chat_identity('123', 'synthetic')
    assert str(err.value) == 'chat identity lookup failed'
    assert err.value.__cause__ is None and err.value.__suppress_context__

@pytest.mark.parametrize('identifier', ['.', '..'])
def test_dot_segments_rejected_before_transport(identifier):
    def forbidden(*a, **kw): raise AssertionError('must not perform GET')
    transport = StreamableHTTPTransport('http://localhost:23373/v0/mcp', opener=forbidden)
    with pytest.raises(BeeperMCPError): transport.resolve_chat_identity(identifier, 'synthetic')

def test_canonical_value_collision_does_not_prove_local_binding():
    with pytest.raises(BeeperMCPError):
        _verified_identity_pair({'id': '123', 'localChatID': 'other'}, '123')

def test_deeply_nested_metadata_fails_closed():
    raw = b'[' * 2000 + b'0' + b']' * 2000
    transport = StreamableHTTPTransport('http://localhost:23373/v0/mcp', opener=lambda *a, **k: Response(raw))
    with pytest.raises(BeeperMCPError):
        transport.resolve_chat_identity('123', 'synthetic')

def test_recursion_error_is_sanitized(monkeypatch):
    def fail(*a, **kw): raise RecursionError('synthetic private metadata')
    transport = StreamableHTTPTransport('http://localhost:23373/v0/mcp', opener=lambda *a, **k: Response(b'{}'))
    monkeypatch.setattr(json, 'loads', fail)
    with pytest.raises(BeeperMCPError) as err:
        transport.resolve_chat_identity('123', 'synthetic')
    assert str(err.value) == 'chat identity lookup failed'
    assert err.value.__cause__ is None and err.value.__suppress_context__

def test_http_protocol_error_is_sanitized():
    import http.client
    def fail(*a, **kw): raise http.client.BadStatusLine('SYNTHETIC_PRIVATE_RESPONSE_CONTENT')
    transport = StreamableHTTPTransport('http://localhost:23373/v0/mcp', opener=fail)
    with pytest.raises(BeeperMCPError) as err:
        transport.resolve_chat_identity('123', 'synthetic')
    assert str(err.value) == 'chat identity lookup failed'
    assert err.value.__cause__ is None and err.value.__suppress_context__
