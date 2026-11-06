"""End-user authentication for the t3mcp adapter (resource-server side).

A short-lived T3 token proves nothing about *which human* is calling; the
``authorize-t3-write`` string literal proves nothing at all (any prompt can
type it). This module implements the verification half of the MCP
authorization spec for the adapter: every ``tools/call`` — reads included —
must present a bearer token naming an end user, and the token must carry
the scope the tool requires (``t3:read`` for reads, ``t3:write`` for writes).

The token is a JWT signed by an authorization server the operator trusts:

* ``RS256`` (recommended): the policy file holds the *public* JWKS only;
  the signing key never touches this machine.
* ``HS256`` (fallback for AS-less personal setups): the shared secret lives
  in an environment variable named by the policy — never in the policy
  file, a log, or a result.

Validation is fail closed. Any missing/ambiguous claim, a bad signature, a
wrong ``iss``/``aud``, or an expired token refuses the call with a stable
code (``auth_required`` / ``auth_invalid`` / ``auth_insufficient_scope``).
The token itself is never logged or echoed; only claim *categories* appear
in error details.
"""

import base64
import hashlib
import hmac
import json
import math
import re
import time

from .errors import AdapterError

READ_SCOPE = "t3:read"
WRITE_SCOPE = "t3:write"
SUPPORTED_ALGS = ("RS256", "HS256")


class AuthContext:
    """The verified end-user identity bound to one tool call."""

    def __init__(self, subject, scopes, expires_at=None, token_id=None):
        self.subject = subject
        self.scopes = frozenset(scopes)
        self.expires_at = expires_at
        self.token_id = token_id

    def has_scope(self, scope):
        return scope in self.scopes

    def as_public(self):
        """Safe to return in results/logs: identity summary, never the token."""
        return {"subject": self.subject, "scopes": sorted(self.scopes)}


# --- parsing helpers (fail closed on anything malformed) ---------------------

def _b64url_decode(part):
    if not isinstance(part, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", part):
        raise AdapterError("auth_invalid", "malformed token: empty segment")
    padding = "=" * (-len(part) % 4)
    try:
        return base64.urlsafe_b64decode(part + padding)
    except Exception as exc:  # noqa: BLE001 — any decode failure is auth failure
        raise AdapterError("auth_invalid", "malformed token: bad base64url") from exc


def _strict_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON member')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('nonstandard JSON constant')


def _json_segment(part, what):
    raw = _b64url_decode(part)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_members,
                           parse_constant=_reject_constant)
    except Exception as exc:  # noqa: BLE001
        raise AdapterError("auth_invalid", f"malformed token: {what} is not JSON") from exc
    if not isinstance(value, dict):
        raise AdapterError("auth_invalid", f"malformed token: {what} is not an object")
    return value


def _int_claim(claims, name):
    value = claims.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or not math.isfinite(value):
        raise AdapterError("auth_invalid", f"token has no valid {name} claim")
    return float(value)


def _scopes_from(claims):
    scope = claims.get("scope")
    scopes = claims.get("scopes")
    if scope is not None and scopes is not None:
        raise AdapterError("auth_invalid", "token has both scope and scopes claims")
    if scope is not None:
        if not isinstance(scope, str):
            raise AdapterError("auth_invalid", "scope claim must be a string")
        return frozenset(scope.split())
    if scopes is not None:
        if not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes):
            raise AdapterError("auth_invalid", "scopes claim must be an array of strings")
        return frozenset(scopes)
    raise AdapterError("auth_invalid", "token carries no scope claim")


# --- RS256 (public keys only) -------------------------------------------------

def _rsa_public_key_from_jwk(jwk):
    """Build a verification key from a public JWK (n/e are public values)."""
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError as exc:
        raise AdapterError(
            "auth_invalid",
            "RS256 verification needs the 'cryptography' package") from exc
    try:
        n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    except KeyError as exc:
        raise AdapterError("auth_invalid", "jwk is missing n/e components") from exc
    # RFC 7518 section 3.3 requires RSA keys of at least 2048 bits.
    # Enforce this at runtime, not only in optional offline preflight.
    if n.bit_length() < 2048:
        raise AdapterError("auth_invalid", "RSA verification keys must be at least 2048 bits")
    try:
        return rsa.RSAPublicNumbers(e, n).public_key()
    except Exception as exc:  # noqa: BLE001
        raise AdapterError("auth_invalid", "jwk does not describe a valid RSA key") from exc


def _rsa_verify(key, signature, signed):
    """PKCS1v15 + SHA256 — the JWS RS256 standard verification."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    try:
        key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise AdapterError("auth_invalid", "token signature verification failed") from exc
    except Exception as exc:  # noqa: BLE001 — any crypto failure is auth failure
        raise AdapterError("auth_invalid", "token signature verification failed") from exc


# --- verifier -------------------------------------------------------------------

class JwtVerifier:
    """Validates end-user JWTs against one configured authorization server."""

    def __init__(self, *, issuer, audience, algorithm, jwks=None,
                 secret_env=None, leeway_s=30, wall_clock=time.time):
        if algorithm not in SUPPORTED_ALGS:
            raise AdapterError("auth_invalid", f"unsupported algorithm {algorithm!r}")
        self.issuer = issuer
        self.audience = audience
        self.algorithm = algorithm
        if isinstance(leeway_s, bool) or not isinstance(leeway_s, (int, float)) \
                or not math.isfinite(leeway_s) or leeway_s < 0:
            raise AdapterError("auth_invalid", "clock leeway must be finite and non-negative")
        self.leeway_s = leeway_s
        self.wall_clock = wall_clock

        if algorithm == "RS256":
            keys = (jwks or {}).get("keys")
            if not isinstance(keys, list) or not keys:
                raise AdapterError("auth_invalid",
                                   "RS256 requires a jwks with at least one public key")
            self._rsa_keys = {}
            for jwk in keys:
                if not isinstance(jwk, dict) or jwk.get("kty") != "RSA":
                    raise AdapterError("auth_invalid", "jwks contains a non-RSA key")
                kid = jwk.get("kid") or "default"
                if kid in self._rsa_keys:
                    raise AdapterError("auth_invalid", f"duplicate kid {kid!r} in jwks")
                self._rsa_keys[kid] = _rsa_public_key_from_jwk(jwk)
            self._hs_secret = None
        else:  # HS256
            if not isinstance(secret_env, str) or not secret_env:
                raise AdapterError("auth_invalid",
                                   "HS256 requires secret_env naming an env var")
            self._rsa_keys = None
            self._hs_secret = _load_hs_secret(secret_env)

    def verify(self, token):
        """Return the AuthContext for a valid token; raise AdapterError else.

        The token value itself never appears in any exception or log line.
        """
        if not isinstance(token, str) or token.count(".") != 2:
            raise AdapterError("auth_invalid", "malformed token: expected a JWS")
        header_b64, payload_b64, sig_b64 = token.split(".")

        header = _json_segment(header_b64, "header")
        # No JOSE critical extension or unencoded-payload processing is supported.
        if "crit" in header or "b64" in header:
            raise AdapterError("auth_invalid", "unsupported JOSE extension")
        if "kid" in header and (not isinstance(header["kid"], str) or not header["kid"]):
            raise AdapterError("auth_invalid", "invalid key identifier")
        alg = header.get("alg")
        if alg != self.algorithm:
            raise AdapterError("auth_invalid", "token algorithm does not match policy")
        claims = _json_segment(payload_b64, "claims")

        signed = (header_b64 + "." + payload_b64).encode("ascii")
        signature = _b64url_decode(sig_b64)
        self._verify_signature(signed, signature, header.get("kid"))

        self._verify_claims(claims)
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise AdapterError("auth_invalid", "token has no subject claim")
        return AuthContext(
            subject=subject,
            scopes=_scopes_from(claims),
            expires_at=_int_claim(claims, "exp"),
            token_id=claims.get("jti"),
        )

    def _verify_signature(self, signed, signature, kid):
        if self.algorithm == "HS256":
            expected = hmac.new(self._hs_secret, signed, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, signature):
                raise AdapterError("auth_invalid", "token signature verification failed")
            return
        # RS256: pick the key by kid, or the single configured key.
        key = None
        if isinstance(kid, str):
            key = self._rsa_keys.get(kid)
        elif len(self._rsa_keys) == 1:
            key = next(iter(self._rsa_keys.values()))
        if key is None:
            raise AdapterError("auth_invalid", "no matching signing key for token")
        _rsa_verify(key, signature, signed)

    def _verify_claims(self, claims):
        iss = claims.get("iss")
        if not isinstance(iss, str) or not hmac.compare_digest(iss, self.issuer):
            raise AdapterError("auth_invalid", "token issuer does not match policy")
        aud = claims.get("aud")
        aud_values = aud if isinstance(aud, list) else [aud]
        if not isinstance(aud, (str, list)) or not aud_values \
                or any(not isinstance(a, str) for a in aud_values) \
                or self.audience not in aud_values:
            raise AdapterError("auth_invalid",
                               "token audience does not match this adapter")
        now = self.wall_clock()
        exp = _int_claim(claims, "exp")
        if now > exp + self.leeway_s:
            raise AdapterError("auth_invalid", "token has expired")
        nbf = claims.get("nbf")
        if nbf is not None and now + self.leeway_s < _int_claim(claims, "nbf"):
            raise AdapterError("auth_invalid", "token is not yet valid")


def _load_hs_secret(secret_env):
    import os
    secret = os.environ.get(secret_env)
    if not isinstance(secret, str) or len(secret) < 16:
        raise AdapterError(
            "auth_invalid",
            f"HS256 secret env var {secret_env!r} is missing or too short (<16 chars)")
    return secret.encode("utf-8")
