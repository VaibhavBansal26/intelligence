from __future__ import annotations

import numpy as np
from hypothesis import given
from hypothesis import strategies as st

from app.features.stats import (
    fit_beta_prior,
    lift_by_decile,
    psi_against_reference,
    psi_from_proportions,
    reference_bins,
    wilson_interval,
)


@given(st.integers(0, 5000), st.integers(1, 5000))
def test_wilson_contains_point_estimate_and_is_bounded(s: int, n: int) -> None:
    s = min(s, n)
    lo, hi = wilson_interval(s, n)
    assert 0.0 <= lo <= s / n + 1e-9 and s / n - 1e-9 <= hi <= 1.0


def test_wilson_narrows_with_n() -> None:
    w_small = np.subtract(*wilson_interval(5, 10)[::-1])
    w_big = np.subtract(*wilson_interval(500, 1000)[::-1])
    assert w_big < w_small
    assert wilson_interval(0, 0) == (0.0, 1.0)


@given(st.lists(st.tuples(st.floats(0, 1), st.integers(0, 1)), min_size=10, max_size=300))
def test_lift_deciles_properties(rows: list[tuple[float, int]]) -> None:
    score = np.array([r[0] for r in rows])
    y = np.array([r[1] for r in rows])
    out = lift_by_decile(y, score)
    assert sum(r["n"] for r in out) == len(rows)
    caps = [r["cum_capture"] for r in out]
    assert caps == sorted(caps)
    if y.sum() > 0:
        assert abs(caps[-1] - 1.0) < 1e-9
        assert abs(sum(r["rate"] * r["n"] for r in out) - y.sum()) < 1e-6


def test_lift_perfect_scores_put_all_positives_first() -> None:
    y = np.array([1] * 10 + [0] * 90)
    out = lift_by_decile(y, y.astype(float))
    assert out[0]["rate"] == 1.0 and out[0]["lift"] == 10.0 and out[-1]["rate"] == 0.0


@given(st.lists(st.floats(0.01, 1), min_size=2, max_size=20))
def test_psi_identical_is_zero_and_nonnegative(p: list[float]) -> None:
    assert psi_from_proportions(p, p) < 1e-12
    q = list(reversed(p))
    assert psi_from_proportions(p, q) >= -1e-12


def test_psi_detects_shift_and_ignores_resample() -> None:
    rng = np.random.default_rng(0)
    ref = reference_bins(rng.normal(0, 1, 5000))
    assert ref is not None
    same = psi_against_reference(ref, rng.normal(0, 1, 5000))
    shifted = psi_against_reference(ref, rng.normal(1.5, 1, 5000))
    assert same < 0.05 and shifted > 0.25


def test_beta_prior_shrinks_noise_groups_strongly_and_real_differences_weakly() -> None:
    rng = np.random.default_rng(1)
    n = np.full(30, 200)
    noise = rng.binomial(n, 0.3)
    real = rng.binomial(n, np.linspace(0.05, 0.9, 30))
    assert fit_beta_prior(noise, n).strength > 10 * fit_beta_prior(real, n).strength


def test_tiny_sample_cannot_beat_big_sample_by_luck() -> None:
    """A 0/4 step must not look worse than a well measured 150/500 step after shrinkage."""
    n = np.array([5000, 4000, 3000, 2500, 4, 500])
    s = np.array([1500, 1200, 900, 760, 0, 150])
    prior = fit_beta_prior(s, n)
    post = prior.posterior_mean(s, n)
    assert post[4] > 0.15  # pulled well away from the raw 0%
    assert abs(post[5] - 0.3) < 0.02  # the large sample barely moves
