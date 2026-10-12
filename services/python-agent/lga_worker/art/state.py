"""The state machine of one picture, with a structured log.

RECEIVED -> CLASSIFIED -> REFERENCE_RESOLVED -> ART_ANALYZED -> MEDIUM_SELECTED -> RENDERING_MODE_SELECTED ->
CANVAS_CALIBRATED -> PLAN_CREATED -> PLAN_VALIDATED -> EXECUTING -> VERIFYING <-> CORRECTING -> COMPLETED

Side states: WAITING_FOR_REFERENCE, WAITING_FOR_USER, OFFLINE_FALLBACK, PAUSED, RECOVERABLE_ERROR, FAILED, CANCELLED.
Only a verified result reaches COMPLETED; an illegal jump raises, so a code path can never claim completion it skipped.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class State(str, Enum):
    RECEIVED = "RECEIVED"
    CLASSIFIED = "CLASSIFIED"
    REFERENCE_RESOLVED = "REFERENCE_RESOLVED"
    ART_ANALYZED = "ART_ANALYZED"
    MEDIUM_SELECTED = "MEDIUM_SELECTED"
    RENDERING_MODE_SELECTED = "RENDERING_MODE_SELECTED"
    CANVAS_CALIBRATED = "CANVAS_CALIBRATED"
    PLAN_CREATED = "PLAN_CREATED"
    PLAN_VALIDATED = "PLAN_VALIDATED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    CORRECTING = "CORRECTING"
    COMPLETED = "COMPLETED"
    WAITING_FOR_REFERENCE = "WAITING_FOR_REFERENCE"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    OFFLINE_FALLBACK = "OFFLINE_FALLBACK"
    PAUSED = "PAUSED"
    RECOVERABLE_ERROR = "RECOVERABLE_ERROR"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


MAIN = [State.RECEIVED, State.CLASSIFIED, State.REFERENCE_RESOLVED, State.ART_ANALYZED, State.MEDIUM_SELECTED,
        State.RENDERING_MODE_SELECTED, State.CANVAS_CALIBRATED, State.PLAN_CREATED, State.PLAN_VALIDATED, State.EXECUTING,
        State.VERIFYING, State.COMPLETED]
SIDE = {State.WAITING_FOR_REFERENCE, State.WAITING_FOR_USER, State.OFFLINE_FALLBACK, State.PAUSED, State.RECOVERABLE_ERROR}
TERMINAL = {State.COMPLETED, State.FAILED, State.CANCELLED}
_POSITION = {s: i for i, s in enumerate(MAIN)}


class IllegalTransition(RuntimeError):
    pass


@dataclass
class ArtRun:
    task_id: str = ""
    request: str = ""
    state: State = State.RECEIVED
    started: float = field(default_factory=time.monotonic)
    history: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=lambda: {"actions": 0, "actions_ok": 0, "retries": 0, "corrections": 0})
    errors: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)
    _resume: State | None = None
    _entered: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.history.append({"state": self.state.value, "t": 0.0, "detail": {}})

    # ------------------------------------------------------------------ transitions
    def _allowed(self, new: State) -> bool:
        cur = self.state
        if cur in TERMINAL:
            return False
        if new in (State.FAILED, State.CANCELLED) or new in SIDE:
            return True
        if cur in SIDE:  # leave a side state: back to where we were, or on
            return self._resume is None or new == self._resume or _POSITION.get(new, -1) >= _POSITION.get(self._resume, 0)
        if new == State.CORRECTING:
            return cur in (State.VERIFYING, State.EXECUTING, State.CORRECTING)
        if cur == State.CORRECTING:
            return new in (State.VERIFYING, State.EXECUTING, State.COMPLETED)
        if new == State.EXECUTING and cur in (State.EXECUTING, State.VERIFYING):  # a stage after a verified stage
            return True
        if new == State.VERIFYING and cur == State.EXECUTING:
            return True
        return _POSITION.get(new, -1) > _POSITION.get(cur, 99)

    def go(self, new: State, **detail: Any) -> None:
        if new == State.COMPLETED and self.state not in (State.VERIFYING, State.CORRECTING):
            raise IllegalTransition(f"COMPLETED needs a verification first (state is {self.state.value})")
        if not self._allowed(new):
            raise IllegalTransition(f"{self.state.value} -> {new.value} is not allowed")
        if new in SIDE and self.state not in SIDE:
            self._resume = self.state
        if self.state in SIDE and new not in SIDE:
            self._resume = None
        now = time.monotonic()
        self.history.append({"state": new.value, "t": round(now - self.started, 2), "took": round(now - self._entered, 2),
                             "detail": detail})
        self._entered = now
        self.state = new

    def fail(self, reason: str, **detail: Any) -> None:
        self.errors.append(reason)
        if self.state not in TERMINAL:
            self.go(State.FAILED, reason=reason, **detail)

    def note_error(self, message: str) -> None:
        self.errors.append(message)

    @property
    def done(self) -> bool:
        return self.state in TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "request": self.request, "state": self.state.value,
                "elapsed_s": round(time.monotonic() - self.started, 2), "counters": dict(self.counters),
                "errors": list(self.errors), "history": self.history, **self.info}

    def write(self, directory: Path, stem: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{stem}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        return path
