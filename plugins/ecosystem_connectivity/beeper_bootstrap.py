"""Explicit, non-starting read-only Beeper application composition.

Operators supply trusted public keys and an independent credential provider.
Construction performs no network I/O and never reads ambient credentials.
The HTTP host must still implement the gateway contract in the README.
"""

from collections.abc import Mapping
from urllib.parse import urlsplit

from .beeper_mcp_plugin import (
    BeeperMCPPlugin, BeeperPluginError, JwtCallerVerifier, create_beeper_wsgi_app,
)
from .beeper_mcp_readonly import ReadOnlyBeeperMCP, StreamableHTTPTransport
from .t3mcp_adapter.t3mcp.auth import JwtVerifier


def _https_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise BeeperPluginError("an explicit HTTPS URL is required")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username \
            or parsed.password or parsed.fragment or parsed.query:
        raise BeeperPluginError("an explicit HTTPS URL without credentials is required")
    return value


def _create_plugin(config, *, beeper_token_provider):
    """Build a WSGI callable, without a listener, OAuth flow, or token minting.

    Required config fields: resource, issuer, jwks, chat_entitlements, beeper_url.
    Public keys are pinned snapshots, not automatically fetched or refreshed.
    Entitlements are copied at startup; restart to replace keys or entitlements.
    """
    required = {"resource", "issuer", "jwks", "chat_entitlements", "beeper_url"}
    if not isinstance(config, Mapping) or set(config) != required:
        raise BeeperPluginError("configuration must contain exactly the required fields")
    if not callable(beeper_token_provider):
        raise BeeperPluginError("an independent Beeper token provider is required")
    resource = _https_url(config["resource"])
    issuer = _https_url(config["issuer"])
    jwks = config["jwks"]
    if not isinstance(jwks, Mapping) or not isinstance(jwks.get("keys"), list) \
            or not jwks["keys"]:
        raise BeeperPluginError("trusted RSA public keys are required")
    for key in jwks["keys"]:
        if not isinstance(key, Mapping) or key.get("kty") != "RSA" \
                or any(field in key for field in ("d", "p", "q", "dp", "dq", "qi", "oth", "k")) \
                or key.get("alg", "RS256") != "RS256" \
                or key.get("use", "sig") != "sig" \
                or ("key_ops" in key and key["key_ops"] != ["verify"]):
            raise BeeperPluginError("only RS256 verification public keys are accepted")
    entitlements = config["chat_entitlements"]
    if not isinstance(entitlements, Mapping) or not entitlements:
        raise BeeperPluginError("explicit caller chat entitlements are required")
    snapshot = {}
    for subject, chats in entitlements.items():
        if not isinstance(subject, str) or not subject \
                or not isinstance(chats, (list, tuple, set, frozenset)) \
                or any(not isinstance(chat, str) or not chat for chat in chats):
            raise BeeperPluginError("invalid caller chat entitlements")
        snapshot[subject] = frozenset(chats)
    backend = config["beeper_url"]
    parsed = urlsplit(backend) if isinstance(backend, str) else None
    if parsed is None or parsed.username or parsed.password or parsed.query \
            or parsed.fragment or any(c.isspace() for c in backend):
        raise BeeperPluginError("invalid loopback Beeper URL")
    # Validate the loopback-only transport now, but do not call it.
    StreamableHTTPTransport(backend)
    verifier = JwtCallerVerifier(JwtVerifier(
        issuer=issuer, audience=resource, algorithm="RS256", jwks=jwks,
    ), snapshot, resource=resource)

    def reader_factory(token, allowed_chats):
        return ReadOnlyBeeperMCP(StreamableHTTPTransport(backend),
                                 allowed_chats=allowed_chats)

    plugin = BeeperMCPPlugin(verify_caller=verifier,
                            beeper_token_provider=beeper_token_provider,
                            reader_factory=reader_factory,
                            server_resource=resource)
    return plugin


def create_readonly_beeper_app(config, *, beeper_token_provider):
    """Legacy minimal WSGI wrapper; prefer the secure HTTP boundary."""
    return create_beeper_wsgi_app(_create_plugin(config,
        beeper_token_provider=beeper_token_provider))


def create_secure_readonly_beeper_app(config, *, beeper_token_provider,
                                     allowed_origins=(), **limits):
    """Compose the secure WSGI boundary without starting a listener."""
    from .beeper_http import create_secure_wsgi_app
    plugin = _create_plugin(config, beeper_token_provider=beeper_token_provider)
    return create_secure_wsgi_app(plugin, authorization_server=config["issuer"],
                                 allowed_origins=allowed_origins, **limits)
