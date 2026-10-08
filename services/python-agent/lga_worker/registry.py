"""Tool registry: loads the unified tool contract from tools/*.json and binds handlers to it.

Rules enforced here:
* every tool in the contract has exactly one handler and every handler has a contract entry;
* arguments are validated against `input_schema` before the handler runs;
* handler output is validated against `output_schema` (a mismatch is a worker bug -> INTERNAL);
* a tool whose contract says `verification: "self"` must return a passed verification.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from lga_common.config import Config

from lga_worker.errors import ErrorCode, ToolError

VerificationMode = Literal["self", "observe", "none"]
VerificationStatus = Literal["passed", "failed", "deferred", "not_applicable"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    category: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    timeout_ms: int
    risk_level: str
    requires_confirmation: bool
    idempotent: bool
    supports_rollback: bool
    verification: VerificationMode
    policy_category: str


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ToolOutcome:
    data: dict[str, Any]
    checks: list[Check] = field(default_factory=list)
    artifacts: list[dict[str, str]] = field(default_factory=list)
    observation: str = ""
    backend: str = ""

    def verification_status(self, mode: VerificationMode) -> VerificationStatus:
        if mode == "none":
            return "not_applicable"
        if mode == "observe" and not self.checks:
            return "deferred"
        return "passed" if all(c.passed for c in self.checks) else "failed"


@dataclass
class ToolContext:
    request_id: str
    task_id: str
    step_id: str
    deadline: float  # time.monotonic() based
    config: Config
    services: Any  # lga_worker.services.Services
    cancel_event: threading.Event

    def remaining_s(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def timeout_s(self, requested_ms: int | None, default_s: float) -> float:
        """A per-call timeout bounded by the request deadline."""
        wanted = requested_ms / 1000.0 if requested_ms else default_s
        return min(wanted, self.remaining_s())

    def artifact_dir(self, kind: str) -> Path:
        base = self.config.path("paths.artifacts_dir")
        directory = base / kind / self.task_id if kind == "screenshots" else base / self.task_id / kind
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise ToolError(ErrorCode.CANCELLED, "cancelled by the core")


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolOutcome]]


def _with_defs(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    if not defs:
        return schema
    merged = dict(schema)
    merged["$defs"] = {**defs, **schema.get("$defs", {})}
    return merged


def load_specs(tools_dir: Path) -> dict[str, ToolSpec]:
    specs: dict[str, ToolSpec] = {}
    files = sorted(tools_dir.glob("*.json"))
    if not files:
        raise FileNotFoundError(f"no tool contract files in {tools_dir}")
    for path in files:
        document = json.loads(path.read_text(encoding="utf-8"))
        defs = document.get("$defs", {})
        for raw in document["tools"]:
            name = raw["name"]
            if name in specs:
                raise ValueError(f"tool {name} is defined twice ({path.name})")
            spec = ToolSpec(
                name=name, description=raw["description"], category=document["category"],
                input_schema=_with_defs(raw["input_schema"], defs), output_schema=_with_defs(raw["output_schema"], defs),
                timeout_ms=int(raw["timeout_ms"]), risk_level=raw["risk_level"],
                requires_confirmation=bool(raw["requires_confirmation"]), idempotent=bool(raw["idempotent"]),
                supports_rollback=bool(raw["supports_rollback"]), verification=raw["verification"],
                policy_category=raw["policy_category"],
            )
            Draft202012Validator.check_schema(spec.input_schema)
            Draft202012Validator.check_schema(spec.output_schema)
            specs[name] = spec
    return specs


def _apply_defaults(schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    filled = dict(args)
    for key, prop in (schema.get("properties") or {}).items():
        if key not in filled and isinstance(prop, dict) and "default" in prop:
            filled[key] = prop["default"]
    return filled


class ToolRegistry:
    def __init__(self, specs: dict[str, ToolSpec]) -> None:
        self._specs = specs
        self._handlers: dict[str, Handler] = {}
        self._input_validators = {n: Draft202012Validator(s.input_schema) for n, s in specs.items()}
        self._output_validators = {n: Draft202012Validator(s.output_schema) for n, s in specs.items()}

    @property
    def specs(self) -> dict[str, ToolSpec]:
        return self._specs

    def handler(self, name: str) -> Callable[[Handler], Handler]:
        def register(fn: Handler) -> Handler:
            if name not in self._specs:
                raise KeyError(f"handler registered for {name}, which is not in the tool contract")
            if name in self._handlers:
                raise KeyError(f"duplicate handler for {name}")
            self._handlers[name] = fn
            return fn

        return register

    def unbound_tools(self) -> list[str]:
        return sorted(set(self._specs) - set(self._handlers))

    def available_tools(self) -> list[str]:
        return sorted(self._handlers)

    async def invoke(self, ctx: ToolContext, tool: str, args: dict[str, Any]) -> tuple[ToolSpec, ToolOutcome]:
        spec = self._specs.get(tool)
        handler = self._handlers.get(tool)
        if spec is None or handler is None:
            raise ToolError(ErrorCode.UNKNOWN_TOOL, f"unknown or unavailable tool {tool!r}")
        try:
            self._input_validators[tool].validate(args)
        except ValidationError as exc:
            location = "/".join(str(p) for p in exc.absolute_path) or "(root)"
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{location}: {exc.message}") from exc
        ctx.check_cancelled()
        outcome = await asyncio.wait_for(handler(ctx, _apply_defaults(spec.input_schema, args)),
                                         timeout=max(0.001, ctx.remaining_s()))
        try:
            self._output_validators[tool].validate(outcome.data)
        except ValidationError as exc:
            raise ToolError(ErrorCode.INTERNAL, f"{tool} produced output violating its contract: {exc.message}") from exc
        if spec.verification == "self" and outcome.verification_status(spec.verification) != "passed":
            failed = [c for c in outcome.checks if not c.passed]
            raise ToolError(ErrorCode.VERIFICATION_FAILED,
                            f"{tool} post-condition failed: " + "; ".join(f"{c.name}: {c.detail}" for c in failed),
                            details={"checks": [c.__dict__ for c in outcome.checks], "data": outcome.data})
        return spec, outcome
