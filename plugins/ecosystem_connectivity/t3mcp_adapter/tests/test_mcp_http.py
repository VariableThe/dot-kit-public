import json
import json as jsonlib
import asyncio

from helpers_t3mcp import AS, auth_config, make_ctx
from t3mcp.mcp_http import MCPResourceServer, create_app, create_asgi_app, create_wsgi_app
from t3pipe import T3Client


def request(server, method, path, value, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    return server.handle(method, path, headers, json.dumps(value).encode())


def server():
    auth = auth_config()
    auth["audience"] = "https://adapter.example/mcp"
    return MCPResourceServer(make_ctx(auth_override=auth)[0], resource="https://adapter.example/mcp")


def test_metadata_and_missing_bearer_challenge():
    app = server()
    metadata = app.handle("GET", "/.well-known/oauth-protected-resource")
    assert metadata.status == 200
    assert json.loads(metadata.body)["authorization_servers"]
    response = request(app, "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer ")
    assert "resource_metadata=" in response.headers["WWW-Authenticate"]


def test_http_bearer_is_verified_and_tools_describe_security():
    app = server()
    token = AS.token(scopes=("t3:read",), audience="https://adapter.example/mcp")
    response = request(app, "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token)
    assert response.status == 200
    tools = json.loads(response.body)["result"]["tools"]
    health = next(item for item in tools if item["name"] == "t3_health")
    assert health["_meta"]["securitySchemes"][0]["scopes"] == ["t3:read"]


def test_initialize_requires_supported_protocol_version():
    app = server()
    token = AS.token(scopes=("t3:read",), audience="https://adapter.example/mcp")
    bad = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    negotiated = request(app, "POST", "/mcp", bad, token)
    assert negotiated.status == 200
    assert json.loads(negotiated.body)["result"]["protocolVersion"] == "2025-06-18"
    good = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    assert request(app, "POST", "/mcp", good, token).status == 200


def test_malformed_protocol_and_ids_fail_cleanly():
    app = server()
    token = AS.token(scopes=("t3:read",), audience="https://adapter.example/mcp")
    for version in ([], {}):
        value = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {}}}
        assert request(app, "POST", "/mcp", value, token).status == 400
    for raw_id in (b"NaN", b"Infinity", b"1.5"):
        raw = b'{"jsonrpc":"2.0","id":' + raw_id + b',"method":"ping"}'
        response = app.handle("POST", "/mcp", {"Authorization": "Bearer " + token}, raw)
        assert response.status == 400
        assert b"NaN" not in response.body and b"Infinity" not in response.body
    raw = b'{"jsonrpc":"2.0","id":NaN,"method":null}'
    response = app.handle("POST", "/mcp", {"Authorization": "Bearer " + token}, raw)
    assert response.status == 400 and b"NaN" not in response.body


def test_deep_json_returns_bad_request_not_server_exception():
    app = server()
    token = AS.token(scopes=("t3:read",), audience="https://adapter.example/mcp")
    raw = (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":' + b'[' * 2000 + b']' * 2000 + b'}')
    assert app.handle("POST", "/mcp", {"Authorization": "Bearer " + token}, raw).status == 400


def test_false_like_params_and_duplicate_auth_are_rejected():
    app = server()
    token = AS.token(scopes=("t3:read",), audience="https://adapter.example/mcp")
    false_params = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":false}'
    assert app.handle("POST", "/mcp", {"Authorization": "Bearer " + token}, false_params).status == 400
    duplicate_json = b'{"jsonrpc":"2.0","id":1,"method":"ping","method":"tools/list"}'
    assert app.handle("POST", "/mcp", {"Authorization": "Bearer " + token}, duplicate_json).status == 400
    headers = [("Authorization", "Bearer " + token), ("authorization", "Bearer " + token)]
    assert app.handle("POST", "/mcp", headers, b'{"jsonrpc":"2.0","id":1,"method":"ping"}').status == 400


def test_unsafe_url_forms_are_rejected():
    for resource in ("http://remote.example/mcp", "https://user:pass@example/mcp", "https://example/mcp#frag", "https://example:bad/mcp"):
        auth = auth_config()
        auth["audience"] = resource
        ctx, _ = make_ctx(auth_override=auth)
        try:
            MCPResourceServer(ctx, resource=resource)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe resource URL was accepted")


def test_constructor_rejects_oauth_mismatch_and_disabled_auth():
    ctx, _ = make_ctx()
    for kwargs in ({"resource": "https://adapter.example/mcp"},
                   {"resource": "https://adapter.example/mcp", "authorization_server": "https://other.example"}):
        try:
            MCPResourceServer(ctx, **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError("incompatible OAuth configuration was accepted")
    disabled, _ = make_ctx(auth=False)
    try:
        MCPResourceServer(disabled, resource="https://adapter.example/mcp")
    except ValueError as exc:
        assert "auth.required" in str(exc)
    else:
        raise AssertionError("auth-disabled context was accepted")


def test_matching_resource_audience_passes_before_issuer_mismatch():
    auth = auth_config()
    auth["audience"] = "https://adapter.example/mcp"
    ctx, _ = make_ctx(auth_override=auth)
    MCPResourceServer(ctx, resource="https://adapter.example/mcp")
    auth["issuer"] = "https://other.example"
    ctx, _ = make_ctx(auth_override=auth)
    try:
        MCPResourceServer(ctx, resource="https://adapter.example/mcp",
                          authorization_server="https://as.example")
    except ValueError as exc:
        assert "issuer" in str(exc)
    else:
        raise AssertionError("issuer mismatch was accepted")


def test_notification_id_and_empty_client_info_are_rejected():
    app = server()
    token = AS.token(scopes=("t3:read",), audience="https://adapter.example/mcp")
    notification = {"jsonrpc": "2.0", "id": 1, "method": "notifications/initialized"}
    assert request(app, "POST", "/mcp", notification, token).status == 400
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}}}
    assert request(app, "POST", "/mcp", init, token).status == 400


def test_scoped_tool_call_succeeds_and_write_scope_is_denied():
    app = server()
    read_token = AS.token(scopes=("t3:read",), audience="https://adapter.example/mcp")
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "t3_health", "arguments": {}}}
    success = request(app, "POST", "/mcp", call, read_token)
    assert success.status == 200
    assert b'"status":"ok"' in success.body
    write = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "t3_start_thread", "arguments": {}}}
    auth = auth_config()
    auth["audience"] = "https://adapter.example/mcp"
    ctx, fake = make_ctx(allow_writes=True, auth_override=auth)
    write_app = MCPResourceServer(ctx, resource="https://adapter.example/mcp")
    denied = request(write_app, "POST", "/mcp", write, read_token)
    assert denied.status == 403
    assert b"insufficient_scope" in denied.body
    assert 'scope="t3:write"' in denied.headers["WWW-Authenticate"]
    assert fake.launches == []


def test_application_factory_is_bounded_in_process():
    app = create_app(server().ctx, resource="https://adapter.example/mcp")
    assert callable(app)


def test_wsgi_adapter_round_trip_and_prebuffer_limit():
    app = create_wsgi_app(server().ctx, resource="https://adapter.example/mcp")
    token = AS.token(scopes=("t3:read",), audience="https://adapter.example/mcp")
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    from io import BytesIO
    captured = {}
    def start(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)
    environ = {"REQUEST_METHOD": "POST", "PATH_INFO": "/mcp",
               "CONTENT_LENGTH": str(len(payload)), "HTTP_AUTHORIZATION": "Bearer " + token,
               "wsgi.input": BytesIO(payload)}
    body = b"".join(app(environ, start))
    assert captured["status"].startswith("200") and b'"tools"' in body


def test_wsgi_reads_exact_content_length_and_does_not_read_get_body():
    app = create_wsgi_app(server().ctx, resource="https://adapter.example/mcp")
    from io import BytesIO
    class Recording(BytesIO):
        def __init__(self, value):
            super().__init__(value)
            self.read_lengths = []
        def read(self, size=-1):
            self.read_lengths.append(size)
            return super().read(size)
    get_stream = Recording(b"unexpected")
    app({"REQUEST_METHOD": "GET", "PATH_INFO": "/.well-known/oauth-protected-resource",
         "wsgi.input": get_stream}, lambda *_: None)
    assert get_stream.read_lengths == []
    post_stream = Recording(b"{}extra")
    result = app({"REQUEST_METHOD": "POST", "PATH_INFO": "/mcp", "CONTENT_LENGTH": "2",
                  "HTTP_AUTHORIZATION": "Bearer bad", "wsgi.input": post_stream}, lambda *_: None)
    assert post_stream.read_lengths == [2]
    assert result


def test_wsgi_rejects_oversized_and_chunked_without_reading():
    app = create_wsgi_app(server().ctx, resource="https://adapter.example/mcp")
    class NoRead:
        def read(self, size=-1):
            raise AssertionError("stream was read")
    for environ, expected in (
        ({"REQUEST_METHOD": "POST", "PATH_INFO": "/mcp", "CONTENT_LENGTH": "1000001", "wsgi.input": NoRead()}, "413"),
        ({"REQUEST_METHOD": "POST", "PATH_INFO": "/mcp", "HTTP_TRANSFER_ENCODING": "chunked", "wsgi.input": NoRead()}, "501"),
    ):
        captured = {}
        app(environ, lambda status, headers: captured.update(status=status))
        assert captured["status"].startswith(expected)


def test_wsgi_rejects_malformed_lengths_and_short_bodies():
    app = create_wsgi_app(server().ctx, resource="https://adapter.example/mcp")
    from io import BytesIO
    for value in (None, "-1", "²", "9" * 21):
        env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/mcp", "wsgi.input": BytesIO(b"")}
        if value is not None:
            env["CONTENT_LENGTH"] = value
        captured = {}
        app(env, lambda status, headers: captured.update(status=status))
        assert captured["status"].startswith("400")
    env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/mcp", "CONTENT_LENGTH": "5", "wsgi.input": BytesIO(b"{}")}
    captured = {}
    app(env, lambda status, headers: captured.update(status=status))
    assert captured["status"].startswith("400")


def test_asgi_streaming_boundary_and_protocol_flow():
    async def exercise():
        app = create_asgi_app(server().ctx, resource="https://adapter.example/mcp", max_body_bytes=1000)
        token = AS.token(scopes=("t3:read",), audience="https://adapter.example/mcp")
        async def call(path, body, headers=None, chunks=None):
            messages = []
            incoming = iter(chunks if chunks is not None else [(body, False)])
            async def receive():
                chunk, more = next(incoming)
                return {"type": "http.request", "body": chunk, "more_body": more}
            async def send(message):
                messages.append(message)
            await app({"type": "http", "method": "POST", "path": path,
                       "headers": headers or []}, receive, send)
            return messages
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
        messages = await call("/mcp", body, [(b"authorization", ("Bearer " + token).encode())], [(body[:10], True), (body[10:], False)])
        assert messages[0]["status"] == 200
        duplicate = await call("/mcp", b"{}", [(b"authorization", b"x"), (b"Authorization", b"y")])
        assert duplicate[0]["status"] == 400
        oversized = await call("/mcp", b"x", [(b"authorization", b"x")], [(b"x" * 1000, True), (b"x", False)])
        assert oversized[0]["status"] == 413
        await app.aclose()
    asyncio.run(exercise())


def test_http_boundary_rejects_oversize_and_untrusted_origin():
    app = server()
    response = app.handle("POST", "/mcp", {"Origin": "https://evil.example"}, b"{}")
    assert response.status == 403
    huge = app.handle("POST", "/mcp", {"Authorization": "Bearer x"}, b"x" * (app.max_body_bytes + 1))
    assert huge.status == 413


def test_deadline_uses_injected_adapter_clock():
    auth = auth_config()
    auth["audience"] = "https://adapter.example/mcp"
    ctx, _ = make_ctx(auth_override=auth, clock=lambda: 100.0)
    app = MCPResourceServer(ctx, resource="https://adapter.example/mcp")
    token = AS.token(scopes=("t3:read",), audience="https://adapter.example/mcp")
    value = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "t3_health", "arguments": {}}}
    response = app.handle("POST", "/mcp", {"Authorization": "Bearer " + token},
                          json.dumps(value).encode(), deadline=99)
    assert response.status == 200
    assert b"deadline_exceeded" in response.body


def test_t3_client_round_trips_through_in_process_http_facade():
    app = server()
    token = AS.token(scopes=("t3:read",), audience="https://adapter.example/mcp")

    class Response:
        def __init__(self, result):
            self.status_code = result.status
            self.headers = result.headers
            self.text = result.body.decode()

    class Session:
        def post(self, url, json, headers, timeout, allow_redirects):
            result = app.handle("POST", "/mcp", headers, jsonlib.dumps(json).encode())
            return Response(result)

    client = T3Client("https://adapter.example/mcp", "unused", session=Session())
    # The facade receives the real external bearer header through the fake
    # session, while no socket is opened.
    client.token = token
    assert client.list_tools()


def test_model_supplied_meta_authorization_is_not_accepted():
    app = server()
    token = AS.token(scopes=("t3:read",), audience="https://adapter.example/mcp")
    value = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "t3_health", "arguments": {},
                         "_meta": {"authorization": token}}}
    response = request(app, "POST", "/mcp", value, token)
    assert response.status == 400
    assert b"HTTP Bearer" in response.body


def test_invalid_http_bearer_returns_challenge_without_echoing_token():
    app = server()
    token = "bad.secret.value"
    value = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "t3_health", "arguments": {}}}
    response = request(app, "POST", "/mcp", value, token)
    assert response.status == 401
    assert token not in response.body.decode()
    assert "invalid_token" in response.headers["WWW-Authenticate"]


def test_initialize_and_tools_list_reject_unverifiable_bearer():
    app = server()
    for method in ("initialize", "tools/list"):
        response = request(app, "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": method}, "not-a-jwt")
        assert response.status == 401

