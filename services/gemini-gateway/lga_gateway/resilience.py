"""Circuit breaker and exponential backoff used by the gateway."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from enum import StrEnum


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class BreakerSettings:
    failure_threshold: int
    open_duration_s: float
    max_open_duration_s: float


class CircuitBreaker:
    """Classic three-state breaker.

    CLOSED: calls pass; `failure_threshold` consecutive failures open the breaker.
    OPEN: calls are rejected until `open_duration` elapsed, then one probe is allowed (HALF_OPEN).
    HALF_OPEN: the probe's success closes the breaker; its failure re-opens it with a doubled open
    duration, capped at `max_open_duration`.
    """

    def __init__(self, settings: BreakerSettings, clock=time.monotonic) -> None:
        self._s = settings
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._open_duration = settings.open_duration_s
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        if self._state is BreakerState.OPEN and self._clock() - self._opened_at >= self._open_duration:
            return BreakerState.HALF_OPEN
        return self._state

    def seconds_until_probe(self) -> float:
        if self._state is not BreakerState.OPEN:
            return 0.0
        return max(0.0, self._open_duration - (self._clock() - self._opened_at))

    def allow(self) -> bool:
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.HALF_OPEN and not self._probe_in_flight:
            self._state = BreakerState.HALF_OPEN
            self._probe_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._probe_in_flight = False
        self._open_duration = self._s.open_duration_s

    def record_failure(self) -> None:
        if self._state is BreakerState.HALF_OPEN or self.state is BreakerState.HALF_OPEN:
            self._open_duration = min(self._open_duration * 2, self._s.max_open_duration_s)
            self._trip()
            return
        self._failures += 1
        if self._failures >= self._s.failure_threshold:
            self._trip()

    def release_probe(self) -> None:
        """The probe ended without a verdict (e.g. capability mismatch): allow another probe."""
        self._probe_in_flight = False

    def force_open(self) -> None:
        """Open immediately (used when the managed process is known to be dead)."""
        self._trip()

    def _trip(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._failures = 0
        self._probe_in_flight = False

    def snapshot(self) -> dict[str, object]:
        return {"state": str(self.state), "consecutive_failures": self._failures,
                "seconds_until_probe": round(self.seconds_until_probe(), 1)}


@dataclass(frozen=True)
class BackoffPolicy:
    max_attempts: int
    initial_s: float
    max_s: float
    multiplier: float
    jitter: float

    def delay(self, attempt: int, rng: random.Random | None = None) -> float:
        """Delay before retry number `attempt` (1-based), with symmetric jitter."""
        base = min(self.initial_s * (self.multiplier ** (attempt - 1)), self.max_s)
        spread = base * self.jitter
        r = (rng or random).uniform(-spread, spread)
        return max(0.0, base + r)
