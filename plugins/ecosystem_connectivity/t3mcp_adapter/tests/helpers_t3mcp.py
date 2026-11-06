"""Offline test doubles for the t3mcp hardening tests.

``MiniAS`` is a minimal authorization server used only by tests: it
generates an RSA keypair at import time (never committed), mints end-user
JWTs, and exports the *public* JWKS the adapter policy needs. Also provides
HS256 minting for the fallback verifier path.

Nothing here is production code and no secret persists anywhere: keys and
tokens live only in the test process.
"""

import base64
import hashlib
import hmac
import json
import time

PROJ = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"

TEST_ISSUER = "https://as.test/t3"
TEST_AUDIENCE = "urn:t3mcp:test"
TEST_HS_SECRET_ENV = "T3MCP_TEST_AUTH_SECRET"
TEST_HS_SECRET = "test-only-hs256-secret-0123456789abcdef"

PROJECTS = [
    {"id": PROJ, "title": "project", "workspaceRoot": "/home/x/project"},
    {"id": OTHER, "title": "secret-project", "workspaceRoot": "/home/x/secret"},
]

THREADS = [
    {"threadId": "thread-1", "projectId": PROJ, "title": "Build widget", "status": "running"},
    {"threadId": "thread-2", "projectId": PROJ, "title": "Fix parser", "status": "idle"},
    {"threadId": "thread-9", "projectId": OTHER, "title": "Secret work", "status": "running"},
]

READS = {
    "thread-1": {
        "thread": {"threadId": "thread-1", "projectId": PROJ, "title": "Build widget"},
        "items": [{"position": 1, "type": "message", "text": "hello"}],
        "recentRuns": [],
        "nextPosition": 2,
        "hasMore": False,
    },
}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class MiniAS:
    """Mints end-user JWTs (RS256) and exposes the public JWKS."""

    def __init__(self, issuer=TEST_ISSUER, audience=TEST_AUDIENCE):
        self.issuer = issuer
        self.audience = audience
        from cryptography.hazmat.primitives.asymmetric import rsa
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-key-1"

    def jwks(self):
        from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
        numbers = self._key.public_key().public_numbers()
        return {
            "keys": [{
                "kty": "RSA", "kid": self.kid, "use": "sig", "alg": "RS256",
                "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
            }]
        }

    def token(self, subject="user-1", scopes=("t3:read", "t3:write"),
              expires_in=600, *, issuer=None, audience=None, alg="RS256",
              nbf=None, kid=None):
        now = int(time.time())
        claims = {
            "iss": issuer if issuer is not None else self.issuer,
            "aud": audience if audience is not None else self.audience,
            "sub": subject,
            "iat": now,
            "exp": now + expires_in,
            "scope": " ".join(scopes),
        }
        if nbf is not None:
            claims["nbf"] = nbf
        header = {"alg": alg, "typ": "JWT"}
        if alg == "RS256":
            header["kid"] = kid or self.kid
        elif alg != "HS256":
            raise ValueError(f"unsupported alg {alg!r}")
        signing_input = (_b64url(json.dumps(header).encode())
                         + "." + _b64url(json.dumps(claims).encode()))
        if alg == "RS256":
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            sig = self._key.sign(signing_input.encode(), padding.PKCS1v15(),
                                hashes.SHA256())
        else:
            sig = hmac.new(TEST_HS_SECRET.encode(), signing_input.encode(),
                           hashlib.sha256).digest()
        return signing_input + "." + _b64url(sig)


# One keypair per test process; the public half goes into test policies.
AS = MiniAS()


def auth_config(required=True):
    """Policy auth section for tests (RS256, public JWKS only)."""
    return {
        "required": required,
        "issuer": TEST_ISSUER,
        "audience": TEST_AUDIENCE,
        "algorithm": "RS256",
        "jwks": AS.jwks(),
        "clock_leeway_s": 5,
    }


def auth_meta(token):
    """params._meta carrying the end-user bearer token."""
    return {"_meta": {"authorization": token}}

# ---------------------------------------------------------------------------
# shared context / policy construction
# ---------------------------------------------------------------------------

import tempfile

from t3mcp import Policy, ToolContext
from fake_t3 import FakeT3


def make_cfg(allow_writes=False, auth=True, limits=None, journal_path=None,
             allowed_projects=None, auth_override=None):
    cfg = {
        "hosts": {"workstation": {"url": "http://127.0.0.1:3773/mcp",
                                  "token_env": "T3_TOKEN_TEST"}},
        "allowed_projects": allowed_projects or [PROJ],
        "allow_writes": allow_writes,
    }
    if limits:
        cfg["limits"] = limits
    if journal_path:
        cfg["journal_path"] = str(journal_path)
    cfg["auth"] = auth_override if auth_override is not None \
        else (auth_config(required=True) if auth else {"required": False})
    return cfg


def make_ctx(allow_writes=False, fake=None, clock=None, wall=None,
             policy_limits=None, default_factory=False, auth=True,
             journal=None, journal_path=None, auth_override=None):
    """Build a ToolContext against the fake backend (or the real factory).

    Writes get a fresh durable journal under a temp dir unless one is
    supplied. ``wall`` injects the wall clock the journal uses (for TTL
    tests); ``clock`` injects the deadline clock.
    """
    if journal_path is None and (journal is None and allow_writes):
        journal_path = tempfile.mkdtemp(prefix="t3mcp-test-") + "/journal.jsonl"
    policy = Policy(make_cfg(allow_writes=allow_writes, limits=policy_limits,
                             journal_path=journal_path, auth=auth,
                             auth_override=auth_override))
    kwargs = {"clock": clock or (lambda: 0.0),
              "wall_clock": wall or (lambda: 0.0), "journal": journal}
    if default_factory:
        return ToolContext(policy, **kwargs), None
    fake = fake or FakeT3(PROJECTS, THREADS)
    ctx = ToolContext(policy, client_factory=lambda host_cfg: fake, **kwargs)
    return ctx, fake


def call_params(name, arguments, scopes=("t3:read", "t3:write"), token=None):
    """tools/call params carrying the end-user bearer token in _meta."""
    return {"name": name, "arguments": arguments,
            "_meta": {"authorization": token if token is not None
                      else AS.token(scopes=scopes)}}


def args(**kw):
    base = {"project_id": PROJ}
    base.update(kw)
    return base


DEADLINE = 1e18  # effectively no deadline
