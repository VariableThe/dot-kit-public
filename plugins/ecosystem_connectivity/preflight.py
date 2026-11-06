"""Provider-neutral offline config checks. No runtime, secret, or network access."""
import argparse
import base64
import json
import math
import re
from urllib.parse import urlsplit
from uuid import UUID

MAX_CONFIG_BYTES = 1_000_000
GATEWAY_FIELDS = frozenset(('tls', 'canonical_routing', 'reject_duplicate_headers',
    'finite_io_timeouts', 'bounded_connections', 'bounded_workers', 'process_supervision'))


class ConfigError(ValueError):
    """Fixed diagnostics must never include supplied configuration values."""


def _require(condition, message):
    if not condition:
        raise ConfigError(message)


def _object(value, required, optional=()):
    _require(type(value) is dict and set(required) <= set(value)
             and set(value) <= set(required) | set(optional), 'missing or unknown fields')


def _text(value, maximum=256):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and all(32 < ord(c) < 127 for c in value))


def _url(value, *, loopback=False, upstream=False):
    _require(_text(value, 2048), 'invalid endpoint URL')
    try:
        p = urlsplit(value)
        port = p.port
        local = p.hostname in ('localhost', '127.0.0.1', '::1')
        _require(bool(p.hostname) and not p.username and not p.password
                 and not p.query and not p.fragment and port != 0
                 and not any(c in value for c in ('\\', '"', '<', '>', '%'))
                 and (p.scheme == 'https' or (p.scheme == 'http' and local and (loopback or upstream)))
                 and (not loopback or local), 'invalid endpoint URL')
        return p
    except (ValueError, TypeError):
        raise ConfigError('invalid endpoint URL') from None


def _integer(value):
    _require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,2048}', value),
             'invalid public key encoding')
    try:
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except (ValueError, TypeError):
        raise ConfigError('invalid public key encoding') from None
    _require(bool(raw) and raw[0] != 0 and base64.urlsafe_b64encode(raw).rstrip(b'=').decode() == value,
             'noncanonical public key encoding')
    return int.from_bytes(raw, 'big')


def _jwks(value):
    _object(value, {'keys'})
    keys = value['keys']
    _require(type(keys) is list and 1 <= len(keys) <= 16, 'bounded public JWKS required')
    seen = set()
    for key in keys:
        _object(key, {'kty','kid','n','e'}, {'alg','use','key_ops'})
        _require(key['kty'] == 'RSA' and key.get('alg','RS256') == 'RS256'
                 and key.get('use','sig') == 'sig' and key.get('key_ops',['verify']) == ['verify']
                 and _text(key['kid'],128) and key['kid'] not in seen, 'invalid public RS256 key')
        seen.add(key['kid'])
        n, e = _integer(key['n']), _integer(key['e'])
        _require(2048 <= n.bit_length() <= 8192 and n % 2 == 1
                 and 3 <= e < n and e % 2 == 1 and e.bit_length() <= 32, 'invalid RSA public numbers')


def validate(document):
    """Validate a secret-free plan. Attestations are not verified deployment facts.

    This intentionally accepts a strict subset of runtime configuration: no
    custom runtime limits, writes, ambient auth or arbitrary provider imports.
    """
    _object(document, {'plugin','config','authorization','hosting','credential_provider'})
    plugin, config = document['plugin'], document['config']
    _require(plugin in ('beeper','t3'), 'unsupported plugin')
    common = {'resource','issuer','jwks'}
    _object(config, common | ({'beeper_url','chat_entitlements'} if plugin == 'beeper'
                              else {'hosts','allowed_projects'}))
    resource = _url(config['resource'])
    _url(config['issuer'])
    _jwks(config['jwks'])
    auth = document['authorization']
    _object(auth, {'required','algorithm','issuer','audience','scopes','server_metadata',
                   'pkce_s256','client_registration','subject_authorization'})
    _require(auth['required'] is True and auth['algorithm'] == 'RS256'
             and auth['issuer'] == config['issuer'] and auth['audience'] == config['resource']
             and auth['scopes'] == [plugin + ':read'], 'authorization must bind exact issuer audience and read scope')
    _require(all(auth[k] is True for k in ('server_metadata','pkce_s256','client_registration','subject_authorization')),
             'authorization prerequisites must be explicitly declared')
    hosting = document['hosting']
    _object(hosting, GATEWAY_FIELDS)
    _require(all(v is True for v in hosting.values()), 'gateway prerequisites must be explicitly declared')
    provider = document['credential_provider']
    _object(provider, {'configured','independent_from_caller','account_bound'})
    _require(all(v is True for v in provider.values()), 'credential provider prerequisites must be explicitly declared')
    if plugin == 'beeper':
        _url(config['beeper_url'], loopback=True)
        entitlements = config['chat_entitlements']
        _require(type(entitlements) is dict and 1 <= len(entitlements) <= 1000, 'bounded chat entitlements required')
        for subject, chats in entitlements.items():
            _require(_text(subject) and subject != '*' and type(chats) is list
                     and 1 <= len(chats) <= 1000 and all(_text(c) and c != '*' for c in chats)
                     and len(set(chats)) == len(chats), 'invalid subject chat entitlement')
    else:
        _require(resource.path == '/mcp', 'T3 resource must use /mcp')
        hosts = config['hosts']
        _require(type(hosts) is dict and 1 <= len(hosts) <= 16, 'bounded host allowlist required')
        for name, host in hosts.items():
            _require(isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',name), 'invalid host name')
            _object(host, {'url','credential_ref'})
            p = _url(host['url'], upstream=True)
            _require(not (p.scheme == 'http' and p.hostname == 'localhost'), 'T3 HTTP upstream requires numeric loopback')
            _require(isinstance(host['credential_ref'],str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,128}',host['credential_ref']),
                     'invalid credential reference')
        projects = config['allowed_projects']
        _require(type(projects) is list and 1 <= len(projects) <= 1000, 'bounded project allowlist required')
        try:
            _require(all(isinstance(p,str) and str(UUID(p)) == p for p in projects)
                     and len(set(projects)) == len(projects), 'canonical unique project UUIDs required')
        except (ValueError, TypeError, AttributeError):
            raise ConfigError('canonical unique project UUIDs required') from None
    return {'plugin':plugin, 'configuration_valid':True, 'live_readiness_verified':False,
            'listener_started':False, 'credentials_read':False,
            'unverified':['issuer ownership and key provenance','cryptographic verifier initialization',
                          'gateway enforcement','credential provider and account binding',
                          'OAuth registration and linking','live backend compatibility'],
            'warnings':(['T3 project allowlist is shared across all accepted readers; issuer or trusted gateway must enforce audience access.']
                        if plugin == 't3' else [])}


def _pairs(pairs):
    result = {}
    for key,value in pairs:
        _require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def _float(value):
    number = float(value)
    _require(math.isfinite(number), 'nonfinite JSON number')
    return number


def _constant(value):
    raise ConfigError('nonfinite JSON number')


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default diagnostics echo unknown arguments and values.
        # Deliberately discard that message, including file paths or secrets.
        self.exit(2, 'invalid command-line arguments\n')


def main(argv=None):
    parser = _SafeParser(description='Offline plugin configuration validation only; never starts a listener.')
    parser.add_argument('--config', required=True, help='Explicit secret-free JSON plan file')
    args = parser.parse_args(argv)
    try:
        with open(args.config, 'rb') as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        _require(len(raw) <= MAX_CONFIG_BYTES, 'configuration exceeds byte limit')
        document = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                              parse_float=_float, parse_constant=_constant)
        result = validate(document)
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        # Do not print parser exceptions: they can contain supplied values/paths.
        print(json.dumps({'configuration_valid':False,'error':'configuration rejected'}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
