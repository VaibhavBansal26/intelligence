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


# Real daily page views of one funnel from the integration run (45 days, dates dropped; the last three are a 70% traffic
# collapse). Daily noise is about 27%, far above the Poisson noise of the synthetic world, so ESD and CUSUM stay silent.
REAL_NOISY_FUNNEL = [
    28,
    36,
    50,
    40,
    48,
    49,
    40,
    36,
    26,
    44,
    28,
    60,
    43,
    60,
    15,
    51,
    60,
    57,
    40,
    76,
    48,
    53,
    40,
    47,
    61,
    56,
    55,
    44,
    44,
    48,
    70,
    55,
    80,
    74,
    27,
    60,
    63,
    53,
    63,
    72,
    36,
    68,
    16,
    15,
    15,
]


def test_three_day_collapse_in_noisy_real_traffic_is_reported() -> None:
    res = detect(np.array(REAL_NOISY_FUNNEL, float))
    assert res.status == "ok"
    drops = [e for e in res.episodes if e.direction == "drop"]
    assert len(drops) == 1 and drops[0].detectors == ["collapse"]
    assert (drops[0].start_idx, drops[0].end_idx) == (42, 44)
    assert drops[0].observed < 0.4 * drops[0].expected


def test_the_same_noisy_series_without_the_collapse_raises_nothing() -> None:
    assert detect(np.array(REAL_NOISY_FUNNEL[:42], float)).episodes == []


def test_one_or_two_low_days_and_small_baselines_do_not_trigger_the_collapse_rule() -> None:
    x = np.array(REAL_NOISY_FUNNEL, float)
    x[-1] = 50
    assert detect(x).episodes == []  # only two low days
    tiny = np.array([4, 5, 3, 6, 4, 5, 4] * 6, float)
    tiny[-3:] = 0
    assert [e for e in detect(tiny).episodes if "collapse" in e.detectors] == []  # baseline below collapse_min_baseline
