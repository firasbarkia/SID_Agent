import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class Permit:
    epoch: int
    probe: bool


class CircuitOpen(Exception):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__("Circuit is open")


class CircuitBreaker:
    """Process-local async breaker. A single recovery probe is allowed at a time.

    Epochs prevent late results from old requests from changing a newer circuit.
    This is not a distributed quota limiter; that belongs at the shared boundary.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1 or recovery_seconds <= 0:
            raise ValueError("Circuit thresholds must be positive")
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._retry_at = 0.0
        self._epoch = 0
        self._probe_inflight = False

    async def acquire(self, *, reserved_tokens: int = 0) -> Permit:
        async with self._lock:
            if self._state == CircuitState.OPEN:
                remaining = self._retry_at - self._clock()
                if remaining > 0:
                    raise CircuitOpen(remaining)
                self._state = CircuitState.HALF_OPEN
            probe = self._state == CircuitState.HALF_OPEN
            if probe:
                if self._probe_inflight:
                    raise CircuitOpen(self.recovery_seconds)
                self._probe_inflight = True
            return Permit(self._epoch, probe)

    async def success(self, permit: Permit) -> None:
        async with self._lock:
            if permit.epoch != self._epoch:
                return
            self._failures = 0
            if permit.probe:
                self._state = CircuitState.CLOSED
                self._probe_inflight = False
                self._epoch += 1

    async def failure(
        self, permit: Permit, *, force_open: bool = False, retry_after: float | None = None
    ) -> None:
        async with self._lock:
            if permit.epoch != self._epoch:
                # An already-running request may carry a longer quota cooldown.
                # Extend this outage, but never affect a later recovery cycle.
                if (
                    self._state == CircuitState.OPEN
                    and permit.epoch + 1 == self._epoch
                    and retry_after is not None
                ):
                    self._retry_at = max(self._retry_at, self._clock() + retry_after)
                return
            self._failures += 1
            if permit.probe or force_open or self._failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._retry_at = self._clock() + max(self.recovery_seconds, retry_after or 0)
                self._probe_inflight = False
                self._epoch += 1

    async def abandon(self, permit: Permit) -> None:
        """Cancellation is not an upstream failure and must release a probe."""
        async with self._lock:
            if permit.epoch == self._epoch and permit.probe:
                self._probe_inflight = False

    async def snapshot(self) -> dict[str, str | int | float]:
        async with self._lock:
            return {
                "state": self._state.value,
                "consecutive_failures": self._failures,
                "retry_after_seconds": max(0.0, self._retry_at - self._clock())
                if self._state == CircuitState.OPEN
                else 0.0,
            }
