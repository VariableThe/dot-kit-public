"""t3pipe — a minimal, secure client for inspecting and coordinating T3 Code threads.

Uses T3's own MCP thread interface (Streamable HTTP) directly. No OpenCode
adapter, no session manipulation. Credentials are resolved from the caller's
environment and never stored or committed.

Public API:
    T3Client        — Streamable HTTP MCP transport + low-level tool calls.
    load_hosts      — host registry (host name -> endpoint + token env var).
    discover        — server info + projects for a host.
    list_threads    — thread listing.
    read_thread     — thread messages / activity.
    start_thread    — dedupe-before-launch, then launch + verify.
    send            — follow-up / steer an existing thread.
    wait_thread     — bounded wait for a run.
    interrupt       — stop a run.
"""

from .client import T3Client
from .hosts import HostConfig, load_hosts, default_hosts
from .ops import (
    discover,
    projects,
    list_threads,
    read_thread,
    start_thread,
    send,
    wait_thread,
    interrupt,
    resolve_project_id,
)

__version__ = "0.1.0"

__all__ = [
    "T3Client",
    "HostConfig",
    "load_hosts",
    "default_hosts",
    "discover",
    "projects",
    "list_threads",
    "read_thread",
    "start_thread",
    "send",
    "wait_thread",
    "interrupt",
    "resolve_project_id",
    "__version__",
]
