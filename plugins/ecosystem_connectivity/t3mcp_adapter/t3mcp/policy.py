"""Private MCP adapter policy: what the exposed tool surface may touch.

Everything the adapter can do is constrained by one JSON policy file
(``T3MCP_CONFIG``, default ``t3mcp.json`` next to the process cwd):

* ``hosts``            — allowlist of T3 MCP endpoints the adapter may use.
* ``allowed_projects``— allowlist of project UUIDs (never titles; titles are
                        ambiguous). Every project-scoped call is checked.
* ``allow_writes``    — master switch. ``false`` (default) hides
                        start/send/interrupt entirely (fail closed).
* ``auth``            — end-user authentication (required by default):
                        every tools/call, reads included, must present a
                        JWT naming the caller and carrying the tool's scope
                        (``t3:read`` / ``t3:write``). A string literal is
                        never treated as consent.
* ``journal_path``    — durable request-outcome journal (required whenever
                        writes are enabled) so uncertain outcomes survive
                        crashes/restarts. Holds request ids + digests, never
                        messages or credentials.
* ``limits``          — size/page/timeout caps so one call cannot pull an
                        unbounded amount of data or run forever.

The T3 credential itself is never in this file: each host names a
``token_env`` environment variable that must be exported by the operator.
Likewise the HS256 auth secret (if used) is referenced by env var only; the
RS256 jwks is public key material.
"""

import json
import os
from pathlib import Path

from t3pipe.hosts import HostConfig

from .errors import AdapterError

DEFAULT_LIMITS = {
    "list_page_size": 50,          # max threads returned per t3_list_threads call
    "membership_scan_pages": 3,    # pages scanned to verify a thread's project
    "membership_scan_page_size": 100,
    "read_page_limit": 50,         # max timeline items per t3_read_thread call
    "max_chars_per_item": 20000,   # max chars per timeline item
    "title_max_chars": 200,
    "message_max_chars": 8000,
    "reason_max_chars": 500,
    "call_timeout_s": 20,          # per HTTP call to T3 (submits must be short)
    "request_budget_s": 75,        # tools/call wall clock; leaves >=45s of the
                                   # tunnel's 120s for hosted queueing + return
    "wait_max_timeout_ms": 60000,  # explicit status checks (t3_wait_thread)
    "wait_default_timeout_ms": 5000,
    "client_request_id_ttl_s": 3600,
    "drain_timeout_s": 10,
}


class PolicyError(AdapterError):
    """The policy file itself is invalid. Fail closed: refuse to start."""

    def __init__(self, message, details=None):
        super().__init__("policy_invalid", message, details)


class Policy:
    def __init__(self, data):
        hosts = data.get("hosts")
        if not isinstance(hosts, dict) or not hosts:
            raise PolicyError("policy must define at least one host")
        self.hosts = {}
        for name, cfg in hosts.items():
            if not isinstance(cfg, dict) or "url" not in cfg:
                raise PolicyError(f"host {name!r} must define a url")
            token_env = cfg.get("token_env") or ("T3_TOKEN_" + name.upper().replace("-", "_"))
            self.hosts[name] = HostConfig(
                name, cfg["url"], token_env, label=cfg.get("label")
            )

        allowed = data.get("allowed_projects")
        if not isinstance(allowed, list) or not allowed or not all(isinstance(p, str) and p for p in allowed):
            raise PolicyError("allowed_projects must be a non-empty list of project UUIDs")
        self.allowed_projects = frozenset(allowed)

        if not isinstance(data.get("allow_writes", False), bool):
            raise PolicyError("allow_writes must be a boolean")
        self.allow_writes = bool(data.get("allow_writes", False))

        limits = dict(DEFAULT_LIMITS)
        extra = data.get("limits") or {}
        if not isinstance(extra, dict):
            raise PolicyError("limits must be an object")
        for key, value in extra.items():
            if key not in limits:
                raise PolicyError(f"unknown limit {key!r}")
            if not isinstance(value, (int, float)) or value <= 0:
                raise PolicyError(f"limit {key!r} must be a positive number")
            limits[key] = value
        self.limits = limits

        self._load_auth(data.get("auth"))

        # Durability: writes must be recoverable across crashes/restarts, so a
        # journal is mandatory whenever writes are enabled. Fail closed.
        journal_path = data.get("journal_path")
        if journal_path is not None and not isinstance(journal_path, str):
            raise PolicyError("journal_path must be a string path or null")
        if self.allow_writes and not journal_path:
            raise PolicyError(
                "allow_writes=true requires journal_path so uncertain writes "
                "stay recoverable after a crash/restart")
        self.journal_path = journal_path

    def _load_auth(self, auth):
        if auth is None:
            # End-user auth is REQUIRED by default; an explicit opt-out must
            # say so. Nothing runs silently unauthenticated.
            raise PolicyError(
                "policy must define an auth section; set {\"required\": false} "
                "explicitly (development only) to run unauthenticated")
        if not isinstance(auth, dict):
            raise PolicyError("auth must be an object")
        required = auth.get("required", True)
        if not isinstance(required, bool):
            raise PolicyError("auth.required must be a boolean")
        self.auth_required = required
        if not self.auth_required:
            # Development-only opt-out: surface it loudly in health.
            self.auth = None
            return
        for field in ("issuer", "audience", "algorithm"):
            if not isinstance(auth.get(field), str) or not auth[field]:
                raise PolicyError(f"auth.{field} is required when auth is required")
        if auth["algorithm"] not in ("RS256", "HS256"):
            raise PolicyError("auth.algorithm must be RS256 or HS256")
        if auth["algorithm"] == "RS256" and not isinstance(auth.get("jwks"), dict):
            raise PolicyError("auth.algorithm RS256 requires auth.jwks (public keys)")
        if auth["algorithm"] == "HS256" and not (
                isinstance(auth.get("secret_env"), str) and auth["secret_env"]):
            raise PolicyError("auth.algorithm HS256 requires auth.secret_env")
        self.auth = {
            "issuer": auth["issuer"],
            "audience": auth["audience"],
            "algorithm": auth["algorithm"],
            "jwks": auth.get("jwks"),
            "secret_env": auth.get("secret_env"),
            "leeway_s": auth.get("clock_leeway_s", 30),
        }

    # --- checks (fail closed) ---------------------------------------------

    def host(self, name=None):
        if name is None:
            return next(iter(self.hosts.values()))
        if name not in self.hosts:
            # A runtime denial, not a policy defect — stable target code.
            raise AdapterError(
                "target_denied",
                "host is not in this adapter's allowlist",
            )
        return self.hosts[name]

    def check_project(self, project_id):
        if not isinstance(project_id, str) or project_id not in self.allowed_projects:
            # Never echo the allowlist contents back into the transcript.
            raise AdapterError(
                "target_denied",
                "project is not in this adapter's allowlist; use t3_list_projects "
                "to see the projects this adapter exposes",
            )
        return project_id

    def limits_get(self, key):
        return self.limits[key]


def load_policy(path=None):
    path = path or os.environ.get("T3MCP_CONFIG") or "t3mcp.json"
    data = json.loads(Path(path).read_text())
    return Policy(data)
