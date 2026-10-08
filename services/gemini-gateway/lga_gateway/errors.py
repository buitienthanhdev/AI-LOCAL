"""Backend error classification. Decides retry vs. failover vs. returning the error to the caller."""

from __future__ import annotations

from enum import StrEnum


class ErrorKind(StrEnum):
    # The backend process/upstream is unreachable or broken: count against the breaker, fail over.
    BACKEND_DOWN = "backend_down"
    # Throttled: back off and retry the same backend, then fail over.
    RATE_LIMITED = "rate_limited"
    # A glitch worth retrying on the same backend (empty reply, transient upstream refusal).
    TRANSIENT = "transient"
    # This backend cannot serve this request (no account for images, model not offered): fail over
    # without penalising the breaker.
    CAPABILITY = "capability"
    # Misconfiguration of the backend (bad local API key): fail over and count against the breaker.
    CONFIG = "config"
    # The request itself is invalid (too long, malformed). Failing over would not help.
    CLIENT_ERROR = "client_error"


FAILOVER_KINDS = {ErrorKind.BACKEND_DOWN, ErrorKind.RATE_LIMITED, ErrorKind.TRANSIENT, ErrorKind.CAPABILITY,
                  ErrorKind.CONFIG}
BREAKER_PENALTY_KINDS = {ErrorKind.BACKEND_DOWN, ErrorKind.RATE_LIMITED, ErrorKind.TRANSIENT, ErrorKind.CONFIG}
RETRY_SAME_BACKEND_KINDS = {ErrorKind.RATE_LIMITED, ErrorKind.TRANSIENT}


class BackendError(Exception):
    def __init__(self, kind: ErrorKind, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status


class GatewayError(Exception):
    """Raised to the gateway's caller when no backend could serve the request."""

    def __init__(self, kind: ErrorKind, message: str, attempts: list | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.attempts = attempts or []
