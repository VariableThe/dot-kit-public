import hashlib
import hmac

import pytest

from plugins.ecosystem_connectivity.beeper_local import (
    BeeperAdapter, BeeperAuthenticator, BeeperError, BeeperPolicy,
)


class FakeBeeper:
    def __init__(self):
        self.sent = []
        self.fail_next = False

    def search_chats(self, query, limit):
        return [{"chat_id": "allowed", "title": query}, {"chat_id": "secret"}][:limit]

    def list_messages(self, chat_id, limit, cursor):
        return {"chat_id": chat_id, "items": [{"id": "m1", "text": "hello"}][:limit], "next_cursor": None}

    def send_message(self, chat_id, text, client_request_id):
        self.sent.append((chat_id, text, client_request_id))
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("response lost")
        return {"accepted": True}


def token(subject, scopes, secret="test-beeper-secret-123"):
    body = f"{subject}|{','.join(scopes)}"
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}|{sig}"


def adapter(monkeypatch, allow_send=False):
    monkeypatch.setenv("BEEPER_PLUGIN_AUTH_SECRET", "test-beeper-secret-123")
    return BeeperAdapter(FakeBeeper(), BeeperPolicy(["allowed"], allow_send=allow_send),
                         BeeperAuthenticator())


def test_read_filters_chats_and_requires_scope(monkeypatch):
    a = adapter(monkeypatch)
    rows = a.search_chats(token("u", ["beeper:read"]), "work")
    assert [row["chat_id"] for row in rows] == ["allowed"]
    with pytest.raises(BeeperError):
        a.search_chats(token("u", ["beeper:send"]), "work")


def test_policy_and_input_types_fail_closed(monkeypatch):
    with pytest.raises(BeeperError):
        BeeperPolicy("allowed")
    with pytest.raises(BeeperError):
        BeeperPolicy(["allowed"], max_items=True)
    a = adapter(monkeypatch)
    with pytest.raises(BeeperError):
        a.search_chats(token("u", ["beeper:read"]), "x" * 257)
    with pytest.raises(BeeperError):
        a.list_messages(token("u", ["beeper:read"]), "allowed", cursor="x" * 257)
    with pytest.raises(BeeperError):
        a.search_chats(token("u", ["beeper:read"]), "x", limit=True)
    with pytest.raises(BeeperError):
        a.list_messages(token("u", ["beeper:read"]), "allowed", limit=False)


def test_malformed_signature_and_message_rows_fail_closed(monkeypatch):
    a = adapter(monkeypatch)
    bad = "u|beeper:read|" + "z" * 64
    with pytest.raises(BeeperError):
        a.search_chats(bad, "x")
    a.backend.list_messages = lambda chat_id, limit, cursor: {
        "chat_id": chat_id, "items": [{"chat_id": "secret", "text": "x"}]}
    with pytest.raises(BeeperError):
        a.list_messages(token("u", ["beeper:read"]), "allowed")
    a.backend.list_messages = lambda chat_id, limit, cursor: {
        "chat_id": chat_id, "items": [{"id": "m1", "text": "x" * 20001}]}
    with pytest.raises(BeeperError):
        a.list_messages(token("u", ["beeper:read"]), "allowed")


def test_alternate_chat_id_cannot_bypass_isolation(monkeypatch):
    a = adapter(monkeypatch)
    a.backend.list_messages = lambda chat_id, limit, cursor: {
        "chat_id": chat_id, "items": [{"id": "m1", "chatID": "secret", "text": "x"}]}
    with pytest.raises(BeeperError):
        a.list_messages(token("u", ["beeper:read"]), "allowed")


def test_messages_reject_unallowlisted_chat(monkeypatch):
    a = adapter(monkeypatch)
    with pytest.raises(BeeperError):
        a.list_messages(token("u", ["beeper:read"]), "secret")


def test_backend_chat_identity_and_response_limits_are_checked(monkeypatch):
    a = adapter(monkeypatch)
    a.backend.list_messages = lambda chat_id, limit, cursor: {
        "chat_id": "secret", "items": [{"id": str(i)} for i in range(100)]}
    with pytest.raises(BeeperError):
        a.list_messages(token("u", ["beeper:read"]), "allowed", limit=1)
    a.backend.search_chats = lambda query, limit: [
        {"chat_id": "allowed", "title": str(i)} for i in range(100)]
    assert len(a.search_chats(token("u", ["beeper:read"]), "x", limit=1)) == 1


def test_backend_extra_fields_are_not_projected(monkeypatch):
    a = adapter(monkeypatch)
    a.backend.search_chats = lambda query, limit: [{
        "chat_id": "allowed", "title": "ok", "messages": ["secret"],
        "access_token": "secret",
    }]
    assert a.search_chats(token("u", ["beeper:read"]), "x") == [
        {"chat_id": "allowed", "title": "ok"}]
    a.backend.list_messages = lambda chat_id, limit, cursor: {
        "chat_id": chat_id,
        "items": [{"id": "m1", "text": "hello", "attachments": ["secret"],
                   "raw_event": {"private": True}}],
        "next_cursor": None,
        "hidden_messages": ["secret"],
    }
    assert a.list_messages(token("u", ["beeper:read"]), "allowed") == {
        "chat_id": "allowed", "items": [{"id": "m1", "text": "hello"}],
        "next_cursor": None,
    }


def test_send_is_disabled_by_default(monkeypatch):
    a = adapter(monkeypatch)
    with pytest.raises(BeeperError):
        a.send_message(token("u", ["beeper:send"]), "allowed", "hi")


def test_enabled_send_is_scoped(monkeypatch):
    a = adapter(monkeypatch, allow_send=True)
    with pytest.raises(BeeperError, match="live backend semantics"):
        a.send_message(token("u", ["beeper:send"]), "allowed", "hi")
    with pytest.raises(BeeperError):
        a.send_message(token("u", ["beeper:send"]), "secret", "hi")


def test_send_remains_disabled_even_with_send_scope(monkeypatch):
    a = adapter(monkeypatch, allow_send=True)
    with pytest.raises(BeeperError, match="live backend semantics"):
        a.send_message(token("u", ["beeper:send"]), "allowed", "hi",
                       client_request_id="explicit-retry-id")
