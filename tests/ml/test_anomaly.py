from __future__ import annotations

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.anomaly import DetectorConfig, cusum_runs, detect, generalized_esd, seasonal_baseline


def series(seed: int, n: int = 120, level: float = 200.0) -> np.ndarray:
    t = np.arange(n)
    lam = level * np.where((t % 7) >= 5, 0.62, 1.0) * (1 + 0.002 * t)
    return np.random.default_rng(seed).poisson(lam).astype(float)


def test_injected_drop_is_found_on_the_right_days() -> None:
    x = series(1)
    x[95:97] *= 0.2
    res = detect(x)
    assert res.status == "ok"
    hit = [e for e in res.episodes if e.direction == "drop" and e.start_idx <= 96 and e.end_idx >= 95]
    assert hit and hit[0].severity > 4
    assert len(res.episodes) == 1


def test_spike_and_level_shift() -> None:
    x = series(2)
    x[80] *= 3
    x[100:] *= 1.8
    res = detect(x)
    kinds = {(e.kind, e.direction) for e in res.episodes}
    assert ("point", "spike") in kinds or ("shift", "spike") in kinds
    assert any(e.direction == "spike" and e.end_idx >= 110 for e in res.episodes)


def test_false_alert_rate_on_clean_seasonal_noise_is_low() -> None:
    total = sum(len(detect(series(s)).episodes) for s in range(100, 160))
    assert total <= 8  # < 0.15 alerts per 120-day series


def test_weekend_dip_is_not_an_anomaly() -> None:
    assert detect(series(3, level=500)).episodes == []


def test_insufficient_history_and_sparse_series() -> None:
    assert detect(series(4, n=15)).status == "insufficient_history"
    x = np.zeros(60)
    x[5] = 3
    assert detect(x).episodes == []


def test_guard_rails_ignore_tiny_absolute_deviations() -> None:
    x = np.full(60, 3.0)
    x[50] = 7.0
    assert detect(x, DetectorConfig(min_abs_dev=5.0)).episodes == []


def test_baseline_is_robust_to_past_anomalies() -> None:
    x = np.full(70, 100.0)
    x[20] = 1000.0
    base = seasonal_baseline(x, 7, 6)
    assert base[27] == 100.0 and base[34] == 100.0


def test_cusum_and_esd_primitives() -> None:
    z = np.concatenate([np.zeros(20), np.full(10, 2.0)])
    runs = cusum_runs(z, 0.5, 5.0)
    assert runs and runs[0][2] == "spike"
    v = np.random.default_rng(0).normal(0, 1, 100)
    v[50] = 12
    assert 50 in generalized_esd(v, 0.01, 5)


@settings(max_examples=40, deadline=None)
@given(st.lists(st.floats(0, 1000, allow_nan=False), min_size=0, max_size=80))
def test_detect_never_crashes_and_episodes_are_ordered(vals: list[float]) -> None:
    res = detect(np.array(vals, float))
    starts = [e.start_idx for e in res.episodes]
    assert starts == sorted(starts)
    assert all(0 <= e.start_idx <= e.end_idx < max(len(vals), 1) for e in res.episodes)


def test_fingerprint_is_stable_for_the_same_episode() -> None:
    x = series(1)
    x[95:97] *= 0.2
    t = np.arange(120, dtype=np.int64) * 86400
    a = detect(x).episodes[0].fingerprint("visits", "all", t)
    x2 = x.copy()
    b = detect(x2).episodes[0].fingerprint("visits", "all", t)
    assert a == b
