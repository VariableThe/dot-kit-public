import io
import json

from plugins.ecosystem_connectivity.beeper_mcp_plugin import (
    BeeperMCPPlugin, Caller, READ_SCOPE, create_beeper_wsgi_app,
)


class Reader:
    def __init__(self, token, chats, *, failure=None):
        self.allowed_chats = chats
        self.token = token
        self.failure = failure

    def initialize(self, token):
        if self.failure:
            raise self.failure

    def list_tools(self):
        return []

    def call_read_tool(self, name, arguments):
        if self.failure:
            raise self.failure
        chat = arguments.get("chatID", "")
        return {"content": [{"type": "text", "text":
                              json.dumps({"chat_id": chat, "messages": []})}]}


def configured(failure=None):
    caller = Caller("alice", frozenset({READ_SCOPE}), frozenset({"alice-chat"}))
    return BeeperMCPPlugin(
        verify_caller=lambda token: caller,
        beeper_token_provider=lambda identity: "backend-token",
        reader_factory=lambda token, chats: Reader(token, chats, failure=failure),
        server_resource="https://dot.example/mcp")


def call_wsgi(app, request, auth="Bearer caller-token", *, method="POST",
              content_type="application/json", content_length=None, raw_body=None):
    raw = json.dumps(request).encode() if request is not None else b"not-json"
    if raw_body is not None:
        raw = raw_body
    environ = {
        "REQUEST_METHOD": method,
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(raw) if content_length is None else content_length),
        "HTTP_AUTHORIZATION": auth,
        "wsgi.input": io.BytesIO(raw),
    }
    captured = {}
    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)
    captured["body"] = b"".join(app(environ, start_response))
    return captured


def test_wsgi_initialize_and_tools_discovery():
    app = create_beeper_wsgi_app(configured())
    response = call_wsgi(app, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert response["status"].startswith("200")
    body = json.loads(response["body"])
    assert body["result"]["capabilities"]["tools"]


def test_wsgi_invalid_bearer_and_per_user_tool_result():
    app = create_beeper_wsgi_app(configured())
    unauthorized = call_wsgi(app, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                             auth="Bearer")
    assert unauthorized["status"].startswith("401")
    valid = call_wsgi(app, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": "list_messages",
                                       "arguments": {"chatID": "alice-chat"}}})
    assert valid["status"].startswith("200")
    outer = json.loads(valid["body"])
    assert '"chat_id": "alice-chat"' in outer["result"]["content"][0]["text"]


def test_wsgi_bad_method_media_size_and_notification():
    app = create_beeper_wsgi_app(configured())
    assert call_wsgi(app, {}, method="GET")["status"].startswith("405")
    assert call_wsgi(app, {}, content_type="text/plain")["status"].startswith("415")
    assert call_wsgi(app, {}, content_length=1_000_001)["status"].startswith("413")
    notification = call_wsgi(app, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert notification["status"].startswith("202")
    assert notification["body"] == b""


def test_wsgi_backend_timeout_and_error_are_bounded():
    for failure in (TimeoutError("timeout"), RuntimeError("backend error")):
        app = create_beeper_wsgi_app(configured(failure=failure))
        response = call_wsgi(app, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": "list_messages",
                                              "arguments": {"chatID": "alice-chat"}}})
        assert response["status"].startswith("200")
        assert json.loads(response["body"])["error"]["code"] == -32000



def test_wsgi_deep_json_is_bounded_before_authentication():
    app = create_beeper_wsgi_app(configured())
    raw = b"[" * 10000 + b"0" + b"]" * 10000
    response = call_wsgi(app, {}, auth=None, raw_body=raw)
    assert response["status"].startswith("401")
    assert json.loads(response["body"])["id"] is None
    authenticated = call_wsgi(app, {}, raw_body=raw)
    assert json.loads(authenticated["body"])["error"]["code"] == -32602


def test_wsgi_lone_surrogate_id_is_serializable_before_authentication():
    app = create_beeper_wsgi_app(configured())
    for auth, status in ((None, "401"), ("Bearer caller-token", "200")):
        response = call_wsgi(app, {"jsonrpc": "2.0", "id": "\ud800",
                                   "method": "tools/list"}, auth=auth)
        assert response["status"].startswith(status)
        assert json.loads(response["body"])["id"] == "\ud800"
        assert int(response["headers"]["Content-Length"]) == len(response["body"])


def test_wsgi_invalid_container_ids_are_not_reflected_in_auth_errors():
    app = create_beeper_wsgi_app(configured())
    for request_id in ({"private": "untrusted"}, [1], True):
        response = call_wsgi(app, {"jsonrpc": "2.0", "id": request_id,
                                   "method": "tools/list"}, auth=None)
        assert response["status"].startswith("401")
        assert json.loads(response["body"])["id"] is None
