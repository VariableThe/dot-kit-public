"""Offline tests for end-user authentication in t3mcp.dispatch.

Requirement: reads AND writes require an authenticated caller. A string
literal is never proof of consent; only a verified bearer JWT naming the
caller (with the required scope) authorizes a tool call. Fail-closed
everywhere, tokens never echoed.
"""

import json
import threading
import time

import pytest

from t3mcp import AdapterError, Policy, PolicyError
from t3mcp.auth import JwtVerifier
from t3mcp.journal import args_digest
from t3mcp.tools import dispatch

from helpers_t3mcp import (
    AS, DEADLINE, PROJ, TEST_HS_SECRET, TEST_HS_SECRET_ENV, TEST_ISSUER,
    TEST_AUDIENCE, args, auth_config, make_ctx,
)
from fake_t3 import BlockingT3


def read_call(ctx, args=None, token="OMIT"):
    return dispatch(ctx, "t3_health", args or {}, DEADLINE,
                    None if token == "OMIT" else token)


def write_call(ctx, arguments, token="OMIT"):
    return dispatch(ctx, "t3_start_thread", arguments, DEADLINE,
                    None if token == "OMIT" else token)


# ---------------------------------------------------------------------------
# reads require an authenticated end user
# ---------------------------------------------------------------------------

def test_read_without_token_is_refused():
    ctx, _ = make_ctx()
    with pytest.raises(AdapterError) as exc:
        read_call(ctx, token=None)
    assert exc.value.code == "auth_required"


def test_read_with_blank_token_is_refused():
    ctx, _ = make_ctx()
    with pytest.raises(AdapterError) as exc:
        read_call(ctx, token="   ")
    assert exc.value.code == "auth_required"


def test_read_with_garbage_token_is_refused():
    ctx, _ = make_ctx()
    with pytest.raises(AdapterError) as exc:
        read_call(ctx, token="not-a-jwt")
    assert exc.value.code == "auth_invalid"


def test_read_with_valid_token_executes():
    ctx, fake = make_ctx()
    r = read_call(ctx, token=AS.token(scopes=("t3:read",)))
    assert r["status"] == "ok"


def test_bearer_prefix_is_accepted():
    ctx, _ = make_ctx()
    r = read_call(ctx, token="Bearer " + AS.token(scopes=("t3:read",)))
    assert r["status"] == "ok"


def test_tampered_signature_is_refused():
    ctx, _ = make_ctx()
    token = AS.token(scopes=("t3:read",))
    header, payload, sig = token.split(".")
    tampered = header + "." + payload + "." + (("B" if sig[0] != "B" else "A") + sig[1:])
    with pytest.raises(AdapterError) as exc:
        read_call(ctx, token=tampered)
    assert exc.value.code == "auth_invalid"


def test_wrong_issuer_is_refused():
    ctx, _ = make_ctx()
    with pytest.raises(AdapterError) as exc:
        read_call(ctx, token=AS.token(scopes=("t3:read",), issuer="https://evil.example"))
    assert exc.value.code == "auth_invalid"


def test_wrong_audience_is_refused():
    ctx, _ = make_ctx()
    with pytest.raises(AdapterError) as exc:
        read_call(ctx, token=AS.token(scopes=("t3:read",), audience="urn:other:service"))
    assert exc.value.code == "auth_invalid"


def test_expired_token_is_refused():
    ctx, _ = make_ctx()  # ctx wall clock is 0.0 in tests
    with pytest.raises(AdapterError) as exc:
        read_call(ctx, token=AS.token(scopes=("t3:read",), expires_in=-10**12))
    assert exc.value.code == "auth_invalid"


def test_not_yet_valid_token_is_refused():
    ctx, _ = make_ctx()
    with pytest.raises(AdapterError) as exc:
        read_call(ctx, token=AS.token(scopes=("t3:read",), nbf=9999999999))
    assert exc.value.code == "auth_invalid"


def test_alg_none_is_refused():
    ctx, _ = make_ctx()
    import base64, json
    def b64(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    none_token = b64({"alg": "none", "typ": "JWT"}) + "." + b64(
        {"iss": TEST_ISSUER, "aud": TEST_AUDIENCE, "sub": "attacker",
         "exp": 9999999999, "scope": "t3:read t3:write"}) + "."
    with pytest.raises(AdapterError) as exc:
        read_call(ctx, token=none_token)
    assert exc.value.code == "auth_invalid"


def test_unknown_signing_key_is_refused():
    ctx, _ = make_ctx()
    from helpers_t3mcp import MiniAS
    stranger = MiniAS().token(scopes=("t3:read",))
    with pytest.raises(AdapterError) as exc:
        read_call(ctx, token=stranger)
    assert exc.value.code == "auth_invalid"


def test_token_is_never_echoed_in_errors():
    ctx, _ = make_ctx()
    token = AS.token(scopes=("t3:read",), audience="urn:wrong:audience")
    with pytest.raises(AdapterError) as exc:
        read_call(ctx, token=token)
    assert token not in str(exc.value)
    assert token.split(".")[1] not in str(exc.value)


# ---------------------------------------------------------------------------
# scope enforcement
# ---------------------------------------------------------------------------

def test_write_tool_with_read_only_scope_is_refused():
    ctx, fake = make_ctx(allow_writes=True)
    with pytest.raises(AdapterError) as exc:
        write_call(ctx, args(title="T", message="M"),
                   token=AS.token(scopes=("t3:read",)))
    assert exc.value.code == "auth_insufficient_scope"
    assert exc.value.details["required_scope"] == "t3:write"
    assert fake.launches == []


def test_read_with_write_only_scope_is_refused():
    ctx, _ = make_ctx()
    with pytest.raises(AdapterError) as exc:
        read_call(ctx, token=AS.token(scopes=("t3:write",)))
    assert exc.value.code == "auth_insufficient_scope"


def test_auth_required_must_be_boolean():
    for value in (None, 0, 1, "false", ""):
        with pytest.raises(PolicyError):
            make_ctx(auth_override={"required": value})


def test_jwt_numeric_dates_and_leeway_must_be_finite():
    with pytest.raises(AdapterError):
        JwtVerifier(issuer=TEST_ISSUER, audience=TEST_AUDIENCE,
                    algorithm="HS256", secret_env=TEST_HS_SECRET_ENV,
                    leeway_s=float("inf"))


def test_concurrent_subjects_use_request_local_journal_identity(tmp_path):
    from helpers_t3mcp import PROJECTS, THREADS
    fake = BlockingT3(PROJECTS, THREADS, block_on="t3_thread_launch")
    launch_barrier = threading.Barrier(2)
    real_call_tool = fake.call_tool

    def synchronized_call(name, args=None, timeout=None, **kwargs):
        if name == "t3_thread_launch":
            launch_barrier.wait(timeout=5)
        return real_call_tool(name, args, timeout, **kwargs)

    fake.call_tool = synchronized_call
    journal_path = tmp_path / "journal.jsonl"
    ctx, _ = make_ctx(allow_writes=True, fake=fake, journal_path=journal_path)
    barrier = threading.Barrier(2)
    results = []

    def run(subject, rid):
        barrier.wait()
        results.append(dispatch(
            ctx, "t3_start_thread",
            args(title="same", message="same", client_request_id=rid),
            DEADLINE, AS.token(subject=subject),
        ))

    a = threading.Thread(target=run, args=("alice", "alice0001"))
    b = threading.Thread(target=run, args=("bob", "bob000001"))
    a.start(); b.start()
    assert fake.reached.wait(timeout=5)
    fake.release.set()
    a.join(5); b.join(5)
    assert len(results) == 2
    records = [json.loads(line) for line in journal_path.read_text().splitlines()]
    digests = {record["digest"] for record in records if record["state"] == "pending"}
    base = {"project_id": PROJ, "title": "same", "message": "same",
            "client_request_id": "ignored"}
    assert args_digest("t3_start_thread", base, host="workstation",
                       subject="alice") in digests
    assert args_digest("t3_start_thread", base, host="workstation",
                       subject="bob") in digests


def test_write_with_write_scope_executes():
    ctx, fake = make_ctx(allow_writes=True)
    r = write_call(ctx, args(title="Authed", message="M"),
                   token=AS.token(scopes=("t3:read", "t3:write")))
    assert r["outcome"] == "launched"
    assert len(fake.launches) == 1


def test_scopes_claim_and_scope_claim_both_accepted():
    # a real AS may use either claim name; a bogus combined value is refused
    ctx, _ = make_ctx()
    assert read_call(ctx, token=AS.token(scopes=("t3:read",)))["status"] == "ok"


# ---------------------------------------------------------------------------
# disabled writes are refused before authentication details matter
# ---------------------------------------------------------------------------

def test_write_tool_refused_by_name_when_writes_disabled():
    ctx, fake = make_ctx(allow_writes=False)
    with pytest.raises(AdapterError) as exc:
        write_call(ctx, args(title="T", message="M"),
                   token=AS.token(scopes=("t3:read", "t3:write")))
    assert exc.value.code == "writes_disabled"
    assert fake.launches == []


def test_unknown_tool_is_refused():
    ctx, _ = make_ctx()
    with pytest.raises(AdapterError) as exc:
        dispatch(ctx, "t3_destroy_everything", {}, DEADLINE, token=AS.token())
    assert exc.value.code == "unknown_tool"


def test_non_object_arguments_refused():
    ctx, _ = make_ctx()
    with pytest.raises(AdapterError) as exc:
        dispatch(ctx, "t3_health", "not-an-object", DEADLINE, token=AS.token())
    assert exc.value.code == "invalid_params"


# ---------------------------------------------------------------------------
# authorization targets still enforced with a valid token (belt+braces)
# ---------------------------------------------------------------------------

def test_valid_token_does_not_bypass_project_allowlist():
    ctx, _ = make_ctx(allow_writes=True)
    with pytest.raises(AdapterError) as exc:
        dispatch(ctx, "t3_list_threads",
                 {"project_id": "22222222-2222-2222-2222-222222222222"},
                 DEADLINE, token=AS.token())
    assert exc.value.code == "target_denied"


def test_valid_token_does_not_bypass_thread_membership():
    ctx, _ = make_ctx(allow_writes=True)
    with pytest.raises(AdapterError) as exc:
        dispatch(ctx, "t3_read_thread",
                 {"project_id": PROJ, "thread_id": "thread-9"},
                 DEADLINE, token=AS.token())
    assert exc.value.code == "thread_not_in_project"


# ---------------------------------------------------------------------------
# explicit development opt-out, HS256 fallback
# ---------------------------------------------------------------------------

def test_auth_opt_out_is_explicit_and_loud():
    ctx, fake = make_ctx(auth=False)   # policy must say {"required": false}
    assert ctx.policy.auth_required is False
    # unauthenticated call proceeds only because the policy opted out
    assert read_call(ctx, token=None)["status"] == "ok"
    # and the health tool reports it, so it is visible in-band
    r = read_call(ctx, token=None)
    assert r["end_user_auth_required"] is False


def test_hs256_valid_token(monkeypatch):
    monkeypatch.setenv(TEST_HS_SECRET_ENV, TEST_HS_SECRET)
    cfg_auth = {
        "required": True,
        "issuer": TEST_ISSUER,
        "audience": TEST_AUDIENCE,
        "algorithm": "HS256",
        "secret_env": TEST_HS_SECRET_ENV,
    }
    ctx, _ = make_ctx(auth_override=cfg_auth)
    token = AS.token(scopes=("t3:read",), alg="HS256")
    assert read_call(ctx, token=token)["status"] == "ok"


def test_hs256_missing_secret_fails_closed_at_call_time(monkeypatch):
    # the secret env var is unset: the first tools/call fails, and it fails
    # closed — not at startup (so a misconfig can't pass unnoticed either way)
    monkeypatch.delenv(TEST_HS_SECRET_ENV, raising=False)
    cfg_auth = {
        "required": True,
        "issuer": TEST_ISSUER,
        "audience": TEST_AUDIENCE,
        "algorithm": "HS256",
        "secret_env": TEST_HS_SECRET_ENV,
    }
    ctx, _ = make_ctx(auth_override=cfg_auth)   # construction is fine
    with pytest.raises(AdapterError) as exc:
        read_call(ctx, token=AS.token(scopes=("t3:read",), alg="HS256"))
    assert exc.value.code == "auth_invalid"


def test_hs256_wrong_secret_is_refused(monkeypatch):
    monkeypatch.setenv(TEST_HS_SECRET_ENV, "a-completely-different-secret")
    cfg_auth = {
        "required": True,
        "issuer": TEST_ISSUER,
        "audience": TEST_AUDIENCE,
        "algorithm": "HS256",
        "secret_env": TEST_HS_SECRET_ENV,
    }
    ctx, _ = make_ctx(auth_override=cfg_auth)
    with pytest.raises(AdapterError) as exc:
        read_call(ctx, token=AS.token(scopes=("t3:read",), alg="HS256"))
    assert exc.value.code == "auth_invalid"


# ---------------------------------------------------------------------------
# policy validation for the auth section
# ---------------------------------------------------------------------------

def test_policy_rejects_auth_without_issuer_audience_algorithm():
    with pytest.raises(PolicyError):
        Policy({"hosts": {"w": {"url": "http://x/mcp"}},
                "allowed_projects": [PROJ],
                "auth": {"required": True}})


def test_policy_rejects_rs256_without_jwks():
    with pytest.raises(PolicyError):
        Policy({"hosts": {"w": {"url": "http://x/mcp"}},
                "allowed_projects": [PROJ],
                "auth": {"required": True, "issuer": TEST_ISSUER,
                         "audience": TEST_AUDIENCE, "algorithm": "RS256"}})


def test_policy_rejects_hs256_without_secret_env():
    with pytest.raises(PolicyError):
        Policy({"hosts": {"w": {"url": "http://x/mcp"}},
                "allowed_projects": [PROJ],
                "auth": {"required": True, "issuer": TEST_ISSUER,
                         "audience": TEST_AUDIENCE, "algorithm": "HS256"}})


def test_policy_rejects_unknown_auth_algorithm():
    with pytest.raises(PolicyError):
        Policy({"hosts": {"w": {"url": "http://x/mcp"}},
                "allowed_projects": [PROJ],
                "auth": {"required": True, "issuer": TEST_ISSUER,
                         "audience": TEST_AUDIENCE, "algorithm": "none"}})

