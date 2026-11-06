"""t3mcp — private MCP adapter exposing a restricted T3 controller surface.

Hardened: every tools/call requires an end-user JWT (reads included) with
the scope the tool needs (t3:read / t3:write); host/project allowlists are
enforced inside every handler; write outcomes are journaled durably so
uncertain writes stay recoverable across crashes and restarts.
"""

# Keep authentication usable by the Beeper facade without importing T3's
# optional HTTP client dependencies. Public exports retain their names.
from importlib import import_module

_EXPORT_MODULES = {
    "AuthContext": "auth", "JwtVerifier": "auth", "AdapterError": "errors",
    "Journal": "journal", "HTTPResponse": "mcp_http",
    "MCPResourceServer": "mcp_http", "Policy": "policy",
    "PolicyError": "policy", "load_policy": "policy",
    "ToolContext": "tools", "dispatch": "tools", "tool_specs": "tools",
}


def __getattr__(name):
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(name)
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


__all__ = [
    "AdapterError",
    "AuthContext",
    "JwtVerifier",
    "Journal",
    "HTTPResponse",
    "MCPResourceServer",
    "Policy",
    "PolicyError",
    "ToolContext",
    "dispatch",
    "load_policy",
    "tool_specs",
]
