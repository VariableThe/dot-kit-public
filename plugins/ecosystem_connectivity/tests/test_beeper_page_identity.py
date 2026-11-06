"""Synthetic offline tests for captured envelopes, without real user data."""
import json
import pytest
from plugins.ecosystem_connectivity.beeper_mcp_readonly import BeeperMCPError, ReadOnlyBeeperMCP
CHAT = 'synthetic-chat'
def item(**extra): return {'id': 'synthetic-message', 'text': 'synthetic body', **extra}
def page(items, **extra):
    return {'hasMore': True, 'items': items, 'newestCursor': 'opaque-new', 'oldestCursor': 'opaque-old', **extra}
def parse(payload):
    return ReadOnlyBeeperMCP(None, allowed_chats={CHAT})._parse_message_payload(json.dumps(payload), CHAT)
@pytest.mark.parametrize('identity', [{'chatID': CHAT}, {'chat_id': CHAT}, {'chatID': CHAT, 'chat_id': CHAT}])
def test_nonempty_page_proven_by_every_item(identity):
    result = parse(page([item(**identity), {'id': 'second', 'chatID': CHAT}]))
    assert result == {'chat_id': CHAT, 'messages': [{'id': 'synthetic-message', 'text': 'synthetic body'}, {'id': 'second', 'text': ''}], 'has_more': True, 'oldest_cursor': 'opaque-old'}
@pytest.mark.parametrize('items', [[], [item()], [item(chatID=None)], [item(chatID='other')], [item(chatID=CHAT), item()], [item(chatID=CHAT), item(chatID='other')], [item(chatID=CHAT, chat_id='other')], [item(chatID=42)], [item(chatID=CHAT), None]])
def test_absent_top_identity_rejects_unproven_or_mixed_pages(items):
    with pytest.raises(BeeperMCPError): parse(page(items))
@pytest.mark.parametrize('top', [{'chatID': 'other'}, {'chat_id': 'other'}, {'chatID': CHAT, 'chat_id': 'other'}])
def test_top_level_mismatch_is_not_overridden_by_items(top):
    with pytest.raises(BeeperMCPError): parse(page([item(chatID=CHAT)], **top))
def test_matching_top_identity_keeps_existing_empty_page_support():
    assert parse(page([], chatID=CHAT))['messages'] == []
def test_matching_top_identity_keeps_existing_optional_item_identity():
    assert parse(page([item()], chatID=CHAT))['chat_id'] == CHAT
def test_actual_call_path_projects_only_safe_fields():
    class Fake:
        def post(self, payload, token):
            assert payload['params']['arguments'] == {'chatID': CHAT}
            return {'id': payload['id'], 'result': {'content': [{'type': 'text', 'text': json.dumps(page([item(chatID=CHAT, extra='not projected')]))}]}}
    client = ReadOnlyBeeperMCP(Fake(), allowed_chats={CHAT})
    client._initialized = True
    client.transport_token = 'synthetic'
    client._schemas = {'list_messages': {'properties': {'chatID': {'type': 'string'}}, 'required': ['chatID']}}
    result = client.call_read_tool('list_messages', {'chatID': CHAT})
    body = json.loads(result['content'][0]['text'])
    assert body['messages'] == [{'id': 'synthetic-message', 'text': 'synthetic body'}]
    assert body['chat_id'] == CHAT
