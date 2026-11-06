"""Stable error taxonomy for the T3 thread pipeline.

Every failure maps to a stable ``code`` the caller (dot, a human, or a test)
can branch on. Messages carry the actionable detail; ``details`` carries the
raw context (e.g. the tool's ``{_tag, code, message}`` triple) for logging.
"""


class T3Error(Exception):
    """Base error. Subclasses fix the code; callers may override per-instance."""

    code = "t3_error"

    def __init__(self, message, *, code=None, details=None):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.details = details or {}

    def as_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}


class AuthError(T3Error):
    """401 or a missing credential: expired/revoked token, wrong subject, or none set."""

    code = "auth_error"


class ConnectionError(T3Error):
    """Server unreachable, connection refused, or a transport timeout."""

    code = "connection_error"


class ProtocolError(T3Error):
    """Handshake or response body could not be parsed as MCP JSON/SSE."""

    code = "protocol_error"


class ToolError(T3Error):
    """The T3 tool returned ``isError: true`` (e.g. ``invalid_request``,
    ``target_required``). Carries the tool's ``_tag`` and ``code``."""

    code = "tool_error"

    def __init__(self, message, *, tag=None, t3_code=None, details=None):
        super().__init__(message, details=details)
        self.tag = tag
        self.t3_code = t3_code


class TargetRequired(T3Error):
    """A required host/project/thread identifier was not supplied explicitly."""

    code = "target_required"


class ScopeDenied(T3Error):
    """The credential lacks the scope needed for this action (e.g. only
    ``orchestration:read`` on a launch/send/interrupt)."""

    code = "scope_denied"


class Uncertain(T3Error):
    """A launch/send outcome is unknown (response lost / network error).
    The caller must verify state before retrying; never blind-retry."""

    code = "uncertain"


class TimedOut(T3Error):
    """A bounded wait elapsed. The thread is still working — not a failure."""

    code = "timed_out"
