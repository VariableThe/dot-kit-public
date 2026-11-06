"""Strict, non-starting read-only runtime composition. No listener or discovery.

The operator supplies public RS256 keys and a trusted credential provider.
Credentials are fetched once at construction; rebuild after key/token rotation.
This bootstrap is deliberately narrower than the underlying policy API.
"""
import copy
import math
import re
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID

from .policy import DEFAULT_LIMITS, Policy
from .tools import ToolContext
from .mcp_http import create_asgi_app


class RuntimeConfigError(ValueError):
    """Invalid deployment inputs; diagnostics never include supplied values."""


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise RuntimeConfigError('missing or unknown configuration fields')


def _url(value, *, local=False):
    try:
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError()
        p = urlsplit(value)
        if (not p.hostname or p.username or p.password or p.query or p.fragment
                or '%' in p.netloc or '\\' in value or p.port == 0
                or not (p.scheme == 'https' or (local and p.scheme == 'http'
                         and p.hostname in {'127.0.0.1', '::1'}))):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise RuntimeConfigError('invalid endpoint URL') from None


def _validate(config):
    _object(config, {'resource', 'issuer', 'jwks', 'hosts', 'allowed_projects',
                     'allow_writes', 'limits'},
            {'resource', 'issuer', 'jwks', 'hosts', 'allowed_projects'})
    data = copy.deepcopy(config)
    resource = _url(data['resource'])
    # The current HTTP factory routes /mcp; do not advertise a different path.
    if urlsplit(resource).path != '/mcp':
        raise RuntimeConfigError('resource must use the /mcp path')
    issuer = _url(data['issuer'])
    if data.get('allow_writes', False) is not False:
        raise RuntimeConfigError('this bootstrap supports read-only operation only')
    jwks = data['jwks']
    _object(jwks, {'keys'}, {'keys'})
    if not isinstance(jwks['keys'], list) or not 1 <= len(jwks['keys']) <= 16:
        raise RuntimeConfigError('public JWKS must contain 1 to 16 keys')
    kids = set()
    for key in jwks['keys']:
        _object(key, {'kty', 'kid', 'n', 'e', 'alg', 'use', 'key_ops'}, {'kty', 'kid', 'n', 'e'})
        if (key['kty'] != 'RSA' or key.get('alg', 'RS256') != 'RS256'
                or key.get('use', 'sig') != 'sig' or key.get('key_ops', ['verify']) != ['verify']
                or not isinstance(key['kid'], str) or not key['kid'] or key['kid'] in kids
                or any(not isinstance(key[k], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,2048}', key[k])
                       for k in ('n', 'e'))):
            raise RuntimeConfigError('invalid public RS256 key')
        kids.add(key['kid'])
    projects = data['allowed_projects']
    if not isinstance(projects, list) or not projects or len(projects) > 1000:
        raise RuntimeConfigError('nonempty bounded project allowlist required')
    try:
        if any(not isinstance(p, str) or str(UUID(p)) != p for p in projects):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise RuntimeConfigError('project allowlist must contain canonical UUIDs') from None
    hosts = data['hosts']
    if not isinstance(hosts, dict) or not 1 <= len(hosts) <= 16:
        raise RuntimeConfigError('nonempty bounded host allowlist required')
    for name, host in hosts.items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
            raise RuntimeConfigError('invalid host name')
        _object(host, {'url', 'credential_ref'}, {'url', 'credential_ref'})
        _url(host['url'], local=True)
        if not isinstance(host['credential_ref'], str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,128}', host['credential_ref']):
            raise RuntimeConfigError('invalid credential reference')
    limits = data.get('limits', {})
    _object(limits, DEFAULT_LIMITS)
    for key, value in limits.items():
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not 0 < value <= DEFAULT_LIMITS[key]
                or (not key.endswith('_s') and type(value) is not int)):
            raise RuntimeConfigError('limits must be finite and may only tighten defaults')
    return data, resource, issuer


class RuntimeCloseError(RuntimeError):
    """One or more sessions failed to close; retry aclose to finish cleanup."""


def _close_sessions(pending):
    for name, client in tuple(pending.items()):
        try:
            client.session.close()
        except Exception:
            continue
        del pending[name]
    if pending:
        raise RuntimeCloseError('one or more upstream sessions failed to close') from None


class _BoundedSession:
    """Requests-compatible adapter that bounds decoded upstream body bytes."""
    MAX_RESPONSE_BYTES = 1_000_000

    def __init__(self, session):
        self._session = session
        self._session.trust_env = False

    def close(self):
        self._session.close()

    def post(self, url, *, json, headers, timeout, allow_redirects):
        from t3pipe.errors import ProtocolError
        deadline = time.monotonic() + timeout
        response = self._session.post(url, json=json, headers=headers, timeout=timeout,
                                      allow_redirects=False, stream=True)
        try:
            # Do not buffer bodies for errors/redirects; T3Client classifies status.
            if response.status_code >= 300:
                return SimpleNamespace(status_code=response.status_code,
                                       headers=dict(response.headers), text='')
            declared = response.headers.get('Content-Length')
            if declared is not None:
                if not re.fullmatch(r'[0-9]{1,20}', declared) or int(declared) > self.MAX_RESPONSE_BYTES:
                    raise ProtocolError('upstream response exceeds byte limit')
            body = bytearray()
            for chunk in response.iter_content(chunk_size=16384):
                if time.monotonic() > deadline:
                    raise ProtocolError('upstream response budget exceeded')
                if len(body) + len(chunk) > self.MAX_RESPONSE_BYTES:
                    raise ProtocolError('upstream response exceeds byte limit')
                body.extend(chunk)
            try:
                text = body.decode('utf-8')
            except UnicodeDecodeError:
                raise ProtocolError('upstream response is not UTF-8') from None
            return SimpleNamespace(status_code=response.status_code,
                                   headers=dict(response.headers), text=text)
        finally:
            response.close()


class Runtime:
    """ASGI callable owning sessions; shutdown never closes an active session."""
    def __init__(self, app, clients):
        self._app = app
        self._pending_close = dict(clients)

    def _close_clients(self):
        _close_sessions(self._pending_close)

    async def __call__(self, scope, receive, send):
        async def lifecycle_send(message):
            if message.get('type') == 'lifespan.shutdown.complete':
                try:
                    self._close_clients()
                except RuntimeCloseError:
                    message = {'type': 'lifespan.shutdown.failed',
                               'message': 'Upstream session cleanup incomplete'}
            await send(message)
        await self._app(scope, receive, lifecycle_send)

    async def aclose(self):
        drained = await self._app.aclose()
        if drained:
            self._close_clients()
        return drained


def build_runtime(config, credential_provider):
    """Validate and compose without network I/O or credential creation.

    Provider accepts only a configured credential reference, never caller
    input. Returned upstream credentials remain separate from end-user JWTs.
    The host owns gateway limits, OAuth linking, discovery/key refresh and TLS.
    """
    data, resource, issuer = _validate(config)
    if not callable(credential_provider):
        raise RuntimeConfigError('credential provider required')
    policy = Policy({
        'hosts': {name: {'url': h['url'], 'token_env': 'UNUSED_RUNTIME_PROVIDER'}
                  for name, h in data['hosts'].items()},
        'allowed_projects': data['allowed_projects'], 'allow_writes': False,
        'limits': data.get('limits', {}),
        'auth': {'required': True, 'issuer': issuer, 'audience': resource,
                 'algorithm': 'RS256', 'jwks': data['jwks'], 'clock_leeway_s': 0},
    })
    clients = {}
    ctx = ToolContext(policy, client_factory=lambda host: clients[host.name])
    # Eagerly validate keys/dependency support rather than fail the first call.
    try:
        verifier = ctx._get_verifier()
        if any(key.key_size < 2048 for key in verifier._rsa_keys.values()):
            raise RuntimeConfigError('RSA keys must be at least 2048 bits')
    except Exception:
        raise RuntimeConfigError('RS256 verifier could not initialize') from None
    try:
        import requests
        from t3pipe import T3Client
        for name, host in data['hosts'].items():
            try:
                token = credential_provider(host['credential_ref'])
            except Exception:
                raise RuntimeConfigError('upstream credential unavailable') from None
            if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                    or any(ord(c) < 33 or ord(c) > 126 for c in token)):
                raise RuntimeConfigError('invalid upstream credential')
            session = _BoundedSession(requests.Session())
            try:
                clients[name] = T3Client(host['url'], token, session=session,
                                         timeout=policy.limits['call_timeout_s'])
            except Exception:
                session.close()
                raise RuntimeConfigError('upstream client could not initialize') from None
        app = create_asgi_app(ctx, resource=resource, authorization_server=issuer)
    except Exception:
        _close_sessions(clients)
        raise
    return Runtime(app, clients)
