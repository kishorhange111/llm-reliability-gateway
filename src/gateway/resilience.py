"""Resilience primitives with injectable clocks (deterministic tests).

* ``CircuitBreaker`` — closed → open after ``failure_threshold`` failures within a
  rolling ``window`` seconds; open → half-open after ``cooldown``; half-open admits
  ``half_open_max`` probes; a probe success closes, a probe failure re-opens.
* ``backoff_delays`` — capped exponential backoff with *full jitter*
  (AWS Architecture Blog, "Exponential Backoff and Jitter").
* ``TokenBucket`` — per-tenant request-rate limiting.
* ``BudgetTracker`` — per-tenant spend ceiling over a rolling window.
"""

from __future__ import annotations

import random
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import Enum

Clock = Callable[[], float]


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    window: float = 10.0
    cooldown: float = 5.0
    half_open_max: int = 1
    clock: Clock = time.monotonic
    state: BreakerState = BreakerState.CLOSED
    _failures: deque = field(default_factory=deque)
    _opened_at: float = 0.0
    _probes: int = 0
    transitions: list[tuple[float, str]] = field(default_factory=list)

    def _set(self, s: BreakerState) -> None:
        if s != self.state:
            self.state = s
            self.transitions.append((self.clock(), s.value))

    def allow(self) -> bool:
        now = self.clock()
        if self.state == BreakerState.OPEN:
            if now - self._opened_at >= self.cooldown:
                self._set(BreakerState.HALF_OPEN)
                self._probes = 0
            else:
                return False
        if self.state == BreakerState.HALF_OPEN:
            if self._probes >= self.half_open_max:
                return False
            self._probes += 1
        return True

    def record_success(self) -> None:
        if self.state == BreakerState.HALF_OPEN:
            self._failures.clear()
            self._set(BreakerState.CLOSED)

    def record_failure(self) -> None:
        now = self.clock()
        if self.state == BreakerState.HALF_OPEN:
            self._open(now)
            return
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self.window:
            self._failures.popleft()
        if len(self._failures) >= self.failure_threshold:
            self._open(now)

    def _open(self, now: float) -> None:
        self._opened_at = now
        self._failures.clear()
        self._set(BreakerState.OPEN)


def backoff_delays(
    retries: int, base: float = 0.05, cap: float = 2.0, rng: random.Random | None = None
) -> Iterator[float]:
    """Full jitter: sleep ~ U(0, min(cap, base * 2**attempt))."""
    rng = rng or random.Random()
    for attempt in range(retries):
        yield rng.uniform(0, min(cap, base * 2**attempt))


@dataclass
class TokenBucket:
    rate: float  # tokens per second
    capacity: float
    clock: Clock = time.monotonic
    _tokens: float = -1.0
    _last: float = 0.0

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last = self.clock()

    def try_acquire(self, n: float = 1.0) -> bool:
        now = self.clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False


@dataclass
class BudgetTracker:
    limit_usd: float
    window: float = 86_400.0
    clock: Clock = time.monotonic
    _spend: deque = field(default_factory=deque)

    def spent(self) -> float:
        now = self.clock()
        while self._spend and now - self._spend[0][0] > self.window:
            self._spend.popleft()
        return sum(c for _, c in self._spend)

    def can_spend(self, estimate: float = 0.0) -> bool:
        return self.spent() + estimate <= self.limit_usd

    def record(self, cost: float) -> None:
        self._spend.append((self.clock(), cost))
