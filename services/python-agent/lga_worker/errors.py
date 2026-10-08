"""Deterministic error model shared by every tool (mirrors `ErrorCode` in core/rust-agent)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    NOT_FOUND = "NOT_FOUND"
    WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    STALE_ELEMENT = "STALE_ELEMENT"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    POLICY_DENIED = "POLICY_DENIED"
    ELEVATION_REQUIRED = "ELEVATION_REQUIRED"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    BUSY = "BUSY"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICT = "CONFLICT"
    INTERNAL = "INTERNAL"


# Whether a fresh attempt of the same call can reasonably succeed without changing anything.
DEFAULT_RETRYABLE: dict[ErrorCode, bool] = {
    ErrorCode.INVALID_ARGUMENT: False,
    ErrorCode.UNKNOWN_TOOL: False,
    ErrorCode.NOT_FOUND: False,
    ErrorCode.WINDOW_NOT_FOUND: True,
    ErrorCode.ELEMENT_NOT_FOUND: True,
    ErrorCode.STALE_ELEMENT: True,
    ErrorCode.TIMEOUT: True,
    ErrorCode.CANCELLED: False,
    ErrorCode.POLICY_DENIED: False,
    ErrorCode.ELEVATION_REQUIRED: False,
    ErrorCode.BACKEND_UNAVAILABLE: True,
    ErrorCode.BUSY: True,
    ErrorCode.VERIFICATION_FAILED: True,
    ErrorCode.EXECUTION_FAILED: False,
    ErrorCode.UNSUPPORTED: False,
    ErrorCode.CONFLICT: False,
    ErrorCode.INTERNAL: False,
}


@dataclass
class ToolError(Exception):
    code: ErrorCode
    message: str
    retryable: bool | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")
        if self.retryable is None:
            self.retryable = DEFAULT_RETRYABLE[self.code]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": str(self.code),
            "message": self.message,
            "retryable": bool(self.retryable),
            "details": self.details,
        }
