"""Offline regression using tools/list captured on 2026-11-04.

No output schema or serverInfo was retained. This proves input compatibility
only, not live reads. The fixture preserves the captured JSON verbatim.
"""
import json
from pathlib import Path
import pytest
from plugins.ecosystem_connectivity.beeper_mcp_readonly import BeeperMCPError, ReadOnlyBeeperMCP
from plugins.ecosystem_connectivity.beeper_mcp_plugin import BeeperMCPPlugin

SCHEMAS = json.loads((Path(__file__).parent / 'fixtures/beeper_live_input_schemas.json').read_text())

class CapturedTransport:
    def __init__(self): self.calls = []
    def notify(self, payload, token): pass
    def post(self, payload, token):
        self.calls.append(payload)
        method = payload['method']
        if method == 'initialize': result = {'protocolVersion': '2025-03-26'}
        elif method == 'tools/list': result = {'tools': [{'name': name, 'inputSchema': schema} for name, schema in SCHEMAS.items()]}
        else: result = {'content': []} # Deliberately no inferred output contract.
        return {'jsonrpc': '2.0', 'id': payload['id'], 'result': result}

def reader():
    client = ReadOnlyBeeperMCP(CapturedTransport(), allowed_chats={'synthetic-chat'})
    client.initialize('synthetic-token')
    client.list_tools()
    return client

def test_exact_captured_schema_discovery_and_alias():
    client = reader()
    client.call_read_tool('list_messages', {'chat_id': 'synthetic-chat', 'cursor': 'opaque', 'direction': 'before'})
    assert client.transport.calls[-1]['params']['arguments'] == {'chatID': 'synthetic-chat', 'cursor': 'opaque', 'direction': 'before'}

def test_facade_advertises_only_supported_input_properties():
    for tool in BeeperMCPPlugin._tools():
        assert set(tool['inputSchema']['properties']) <= set(SCHEMAS[tool['name']]['properties'])

def test_search_limit_and_scope_are_supported():
    client = reader()
    args = {'query': 'synthetic', 'scope': 'participants', 'limit': 5}
    client.call_read_tool('search_chats', args)
    assert client.transport.calls[-1]['params']['arguments'] == args

@pytest.mark.parametrize('name,args', [
    ('list_messages', {'chatID': 'synthetic-chat', 'limit': 5}),
    ('list_messages', {'chatID': 'synthetic-chat', 'direction': 'sideways'}),
    ('search_chats', {'query': 'synthetic', 'scope': 'all'}),
    ('search_chats', {'query': 'synthetic', 'limit': True}),
    ('search_chats', {'query': 'synthetic', 'limit': 51}),
    ('search_chats', {'query': 'synthetic', 'limit': 0}),
])
def test_invalid_arguments_never_reach_backend(name,args):
    client = reader()
    count = len(client.transport.calls)
    with pytest.raises(BeeperMCPError): client.call_read_tool(name, args)
    assert len(client.transport.calls) == count

def test_query_remains_deliberately_required_by_narrow_wrapper():
    assert SCHEMAS['search_chats']['required'] == []
    client = reader()
    with pytest.raises(BeeperMCPError, match='query'): client.call_read_tool('search_chats', {})
