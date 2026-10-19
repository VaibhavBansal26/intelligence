"""In-memory token-bucket rate limiter (instance-local; one per app container)."""

from __future__ import annotations

import threading
from collections.abc import Callable


class RateLimiter:
    def __init__(self, per_minute: int, burst: int, clock: Callable[[], float]) -> None:
        self._rate = per_minute / 60.0
        self._burst = float(max(burst, 1))
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self._burst, now))
            tokens = min(self._burst, tokens + (now - last) * self._rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            wait = (1.0 - tokens) / self._rate if self._rate > 0 else 60.0
            if len(self._buckets) > 50_000:  # bound memory
                self._buckets = dict(list(self._buckets.items())[-10_000:])
            return False, wait
