# Offline configuration preflight

From the repository root:

`python -m plugins.ecosystem_connectivity.preflight --config /path/to/secret-free-plan.json`

This command only reads the explicitly named JSON file, up to 1,000,000 bytes.
It never constructs the Beeper/T3 runtime, calls a credential provider, imports
operator-supplied code, starts a listener or performs network I/O. Importing the
module does not read configuration or credentials (normal Python imports may read module
files). The `credentials_read: false` result means no credential store/provider
was accessed; it does not certify that the supplied file contains no secrets.
Only provide a secret-free plan. Exit 0 means the plan passes
these offline checks; exit 2 means rejection. Error output does not include input
values or the file path. The command has no serve/deploy mode.

## Plan format

The top-level object has exactly five fields:

- `plugin`: `beeper` or `t3`.
- `config`: the explicit runtime configuration described below.
- `authorization`: `required: true`, `algorithm: "RS256"`, exact `issuer` and
  `audience` matching config issuer/resource, and `scopes` containing exactly
  `beeper:read` or `t3:read`. Also require literal true for `server_metadata`,
  `pkce_s256`, `client_registration` and `subject_authorization`.
- `hosting`: literal true for `tls`, `canonical_routing`,
  `reject_duplicate_headers`, `finite_io_timeouts`, `bounded_connections`,
  `bounded_workers`, and `process_supervision`.
- `credential_provider`: literal true for `configured`,
  `independent_from_caller`, and `account_bound`. Never put credentials here.

Common config fields: `resource`, `issuer`, and public `jwks`. Resource and issuer
must be explicit HTTPS URLs without credentials, query, fragment or percent
encoding. JWKS must contain 1–16 uniquely identified RSA verification keys with
canonical base64url public numbers and 2048–8192-bit odd moduli. Private key
fields, symmetric keys and unsupported usages are rejected. These are structural
checks, not proof of key provenance, primality or verifier/dependency support.

Beeper additionally requires `beeper_url` restricted to loopback and
`chat_entitlements`, a bounded mapping from explicit subjects to unique nonempty
chat-ID lists. No wildcards or default access. This stricter launch-plan subset
does not accept empty entitlements even though an empty runtime entitlement can
be useful as an explicit deny-all policy.

T3 additionally requires `hosts` (named entries containing `url` and
`credential_ref`) and canonical unique `allowed_projects` UUIDs. Public resource
must use `/mcp`; upstream URLs must be HTTPS or numeric-loopback HTTP. References
name an independently managed credential, never contain its value. The accepted
plan is intentionally narrower than runtime config: no custom limits or
`allow_writes`, even false, are accepted. The composed runtime remains read-only.

## Declarations are not verification

The true-valued prerequisite fields are operator declarations, not permission
requests or evidence that enforcement exists. The output always includes
`live_readiness_verified: false` and lists unverified key provenance, gateway,
credential/account binding, real OAuth and backend compatibility. It never calls
the system production-ready. T3's shared project allowlist is explicitly warned
about: issuer audience authorization or a trusted per-user gateway is still
required for users with different project permissions.

After a pass, independent security review and the launch gates remain necessary.
Do not treat it as approval to create credentials, register OAuth clients, install
or start services, expose endpoints, or access chats/projects. See
[LAUNCH_READINESS.md](LAUNCH_READINESS.md), [BEEPER_HTTP.md](BEEPER_HTTP.md), and
[t3mcp_adapter/RUNTIME.md](t3mcp_adapter/RUNTIME.md).

## Verification

Ten new standard-library unittest cases pass in isolated scratch, including both
plugin plans, no configuration/credential reads or socket access during validate/import, strict auth bindings,
prerequisite declarations, key/URL/entitlement/host rejection, write rejection,
and bounded redacted CLI parsing including malformed arguments. Synthetic key numbers are used only as input
fixtures; no credential is created. Compile checks pass. Full repository tests,
actual runtime construction and live service/OAuth checks were not run.
