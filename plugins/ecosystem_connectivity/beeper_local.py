"""Offline, least-privileged Beeper adapter prototype.

This module deliberately targets a small backend interface rather than
pretending to implement Beeper Desktop's live MCP wire protocol. It is
read-only by default, requires a scoped bearer token, bounds result sizes,
and never logs message content or credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass


class BeeperError(RuntimeError):
    pass


@dataclass(frozen=True)
class BeeperPrincipal:
    subject: str
    scopes: frozenset[str]


class BeeperAuthenticator:
    """Test-only HMAC bearer verifier; not deployment authentication."""

    def __init__(self, secret_env="BEEPER_PLUGIN_AUTH_SECRET"):
        self.secret = os.environ.get(secret_env)
        if not self.secret or len(self.secret) < 16:
            raise BeeperError("authentication secret is missing or too short")

    def verify(self, token, required_scope):
        if not isinstance(token, str) or len(token) > 4096:
            raise BeeperError("invalid bearer token")
        try:
            subject, scopes, signature = token.split("|", 2)
        except (AttributeError, ValueError) as exc:
            raise BeeperError("invalid bearer token") from exc
        signed = f"{subject}|{scopes}".encode()
        expected = hmac.new(self.secret.encode(), signed, hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or len(signature) != len(expected) \
                or any(char not in "0123456789abcdef" for char in signature) \
                or not hmac.compare_digest(signature, expected):
            raise BeeperError("invalid bearer token")
        granted = frozenset(filter(None, scopes.split(",")))
        if not subject or required_scope not in granted:
            raise BeeperError("insufficient scope")
        return BeeperPrincipal(subject, granted)


class BeeperPolicy:
    def __init__(self, allowed_chats, *, allow_send=False, max_items=50):
        if isinstance(allowed_chats, (str, bytes)) or not allowed_chats \
                or any(not isinstance(chat, str) or not chat for chat in allowed_chats):
            raise BeeperError("allowed_chats must be non-empty chat IDs")
        if not isinstance(allow_send, bool) or isinstance(max_items, bool) \
                or not isinstance(max_items, int) or max_items <= 0:
            raise BeeperError("invalid Beeper policy")
        self.allowed_chats = frozenset(allowed_chats)
        self.allow_send = allow_send
        self.max_items = max_items


class BeeperAdapter:
    def __init__(self, backend, policy, authenticator):
        self.backend = backend
        self.policy = policy
        self.authenticator = authenticator

    def search_chats(self, token, query, limit=20):
        self.authenticator.verify(token, "beeper:read")
        if not isinstance(query, str) or len(query) > 256:
            raise BeeperError("invalid query")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise BeeperError("invalid limit")
        limit = max(1, min(limit, self.policy.max_items))
        rows = self.backend.search_chats(query, limit)
        if not isinstance(rows, list):
            raise BeeperError("backend returned an invalid chat list")
        safe = []
        for row in rows[:limit]:
            if not isinstance(row, dict) or row.get("chat_id") not in self.policy.allowed_chats:
                continue
            chat_id = row["chat_id"]
            title = row.get("title", "")
            if not isinstance(title, str) or len(title) > 512:
                raise BeeperError("backend returned an invalid chat title")
            safe.append({"chat_id": chat_id, "title": title})
        return safe

    def list_messages(self, token, chat_id, limit=50, cursor=None):
        self.authenticator.verify(token, "beeper:read")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 256):
            raise BeeperError("invalid cursor")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise BeeperError("invalid limit")
        if chat_id not in self.policy.allowed_chats:
            raise BeeperError("chat is not allowlisted")
        limit = max(1, min(limit, self.policy.max_items))
        result = self.backend.list_messages(chat_id, limit, cursor)
        if not isinstance(result, dict) or result.get("chat_id") != chat_id:
            raise BeeperError("backend returned messages for a different chat")
        items = result.get("items", [])
        if not isinstance(items, list):
            raise BeeperError("backend returned an invalid message list")
        projected = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                raise BeeperError("backend returned an invalid message")
            row_chat_id = item.get("chat_id")
            row_chatID = item.get("chatID")
            if row_chat_id is not None and row_chatID is not None and row_chat_id != row_chatID:
                raise BeeperError("conflicting message chat identities")
            if (row_chat_id if row_chat_id is not None else row_chatID) not in (None, chat_id):
                raise BeeperError("backend returned a message for a different chat")
            message_id = item.get("id")
            text = item.get("text", "")
            if not isinstance(message_id, str) or not message_id or len(message_id) > 256:
                raise BeeperError("backend returned an invalid message id")
            if not isinstance(text, str) or len(text) > 20000:
                raise BeeperError("backend returned an invalid message body")
            sender = item.get("sender")
            if sender is not None and (not isinstance(sender, str) or len(sender) > 256):
                raise BeeperError("backend returned an invalid sender")
            projected.append({"id": message_id, "text": text, **({"sender": sender} if sender is not None else {})})
        next_cursor = result.get("next_cursor")
        if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 256):
            raise BeeperError("backend returned an invalid cursor")
        return {"chat_id": chat_id, "items": projected, "next_cursor": next_cursor}

    def send_message(self, token, chat_id, text, client_request_id=None):
        # Beeper's live idempotency and authorization semantics are not
        # verified here. Never send from this prototype, even if a caller
        # supplies an opt-in policy or a request id.
        raise BeeperError("sending is disabled until live backend semantics are verified")
