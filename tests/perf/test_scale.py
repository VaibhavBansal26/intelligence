"""Bounded-memory proof: ingesting 10x more rows must not grow peak RSS proportionally (run: pytest -m slow)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def bench(rows: int) -> dict[str, float]:
    out = subprocess.run(
        [sys.executable, "-m", "benchmarks.ingest_bench", "--rows", str(rows)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=1500,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_small_ingest_lands_everything() -> None:
    r = bench(20_000)
    assert r["landed"] == 20_000


@pytest.mark.slow
def test_two_million_events_bounded_memory() -> None:
    small = bench(200_000)
    big = bench(2_000_000)
    print("BENCH", json.dumps(small), json.dumps(big))
    assert big["landed"] == 2_000_000 and small["landed"] == 200_000
    assert big["peak_rss_mb"] < small["peak_rss_mb"] * 1.5 + 100  # 10x the rows, ~flat memory
    assert big["peak_rss_mb"] < 600
