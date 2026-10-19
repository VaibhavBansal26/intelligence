"""Injectable clock so time-dependent logic is deterministic in tests."""

from __future__ import annotations

import time
from collections.abc import Callable

Clock = Callable[[], float]


def system_clock() -> float:
    return time.time()


def fixed_clock(value: float) -> Clock:
    return lambda: value
