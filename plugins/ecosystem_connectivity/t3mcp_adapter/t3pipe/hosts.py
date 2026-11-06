"""Host registry: stable host names -> T3 MCP endpoints + credential references.

Tokens are never stored in the registry. Each entry names an environment
variable (``token_env``) the caller must export, so credentials stay local to
the caller's environment and out of any file or commit.

The registry itself is a plain JSON file (see ``hosts.example.json``). A host
name is the stable identifier dot and the human share to continue the same
work on the same machine.
"""

import json
import os
from pathlib import Path

from .errors import AuthError, TargetRequired

DEFAULT_HOST = "local"
# Fallback token env used by the local host when the runtime exports its own
# bearer token (present inside a T3 session). Prefer a least-privilege token.
RUNTIME_TOKEN_ENV = "T3_MCP_BEARER_TOKEN"


class HostConfig:
    def __init__(self, name, url, token_env, *, label=None):
        self.name = name
        self.url = url
        self.token_env = token_env
        self.label = label or name

    @property
    def token(self):
        # Fail closed: a host only ever uses its own explicitly configured
        # credential. Never fall back to an ambient/runtime token, which could
        # send another host's (or the local session's) credential to the wrong
        # endpoint.
        value = os.environ.get(self.token_env)
        if not value:
            raise AuthError(
                f"no credential for host {self.name!r}: export {self.token_env}",
                details={"token_env": self.token_env, "host": self.name},
            )
        return value

    def as_dict(self):
        return {
            "name": self.name,
            "label": self.label,
            "url": self.url,
            "token_env": self.token_env,
        }


def load_hosts(path):
    """Load a hosts registry file. Returns ``dict[name -> HostConfig]``."""
    data = json.loads(Path(path).read_text())
    hosts = {}
    for name, cfg in data.get("hosts", {}).items():
        token_env = cfg.get("token_env") or _default_token_env(name)
        hosts[name] = HostConfig(
            name,
            cfg["url"],
            token_env,
            label=cfg.get("label"),
        )
    return hosts


def default_hosts():
    """Registry for just the local instance, driven by the environment."""
    url = os.environ.get("T3_MCP_URL")
    if not url:
        raise TargetRequired(
            "no host registry and T3_MCP_URL is unset; provide a hosts file or set T3_MCP_URL"
        )
    return {
        DEFAULT_HOST: HostConfig(DEFAULT_HOST, url, RUNTIME_TOKEN_ENV, label="local")
    }


def _default_token_env(name):
    return "T3_TOKEN_" + name.upper().replace("-", "_")
