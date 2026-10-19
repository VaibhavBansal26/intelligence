"""Statistical helpers: Wilson intervals, empirical-Bayes Beta shrinkage, lift, PSI."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray


def wilson_interval(successes: float, n: float, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion; (0, 1) when n == 0."""
    if n <= 0:
        return 0.0, 1.0
    p = min(max(successes / n, 0.0), 1.0)
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class BetaPrior:
    alpha: float
    beta: float

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def strength(self) -> float:
        return self.alpha + self.beta

    def posterior_mean(self, successes: ArrayLike, n: ArrayLike) -> NDArray[np.float64]:
        s = np.asarray(successes, float)
        t = np.asarray(n, float)
        return np.asarray((self.alpha + s) / (self.alpha + self.beta + t), dtype=float)


def fit_beta_prior(successes: ArrayLike, n: ArrayLike, *, min_strength: float = 4.0, max_strength: float = 2000.0) -> BetaPrior:
    """Method-of-moments empirical-Bayes Beta prior from many (successes, n) pairs.

    Subtracts the expected binomial sampling variance from the observed spread of rates, so groups
    that differ only by noise collapse onto a strong prior (heavy shrinkage), while genuinely
    heterogeneous groups keep a weak prior.
    """
    s = np.asarray(successes, float)
    t = np.asarray(n, float)
    keep = t > 0
    s, t = s[keep], t[keep]
    if len(t) == 0:
        return BetaPrior(1.0, 1.0)
    mean = float(s.sum() / t.sum())
    mean = min(max(mean, 1e-4), 1 - 1e-4)
    if len(t) < 3:
        return BetaPrior(mean * min_strength, (1 - mean) * min_strength)
    rates = s / t
    w = t / t.sum()
    var_obs = float(np.sum(w * (rates - mean) ** 2))
    sampling = float(np.sum(w * mean * (1 - mean) / t))
    var_true = max(var_obs - sampling, 1e-9)
    strength = mean * (1 - mean) / var_true - 1.0
    strength = min(max(strength, min_strength), max_strength)
    return BetaPrior(mean * strength, (1 - mean) * strength)


def lift_by_decile(y_true: ArrayLike, score: ArrayLike, n_bins: int = 10) -> list[dict[str, float]]:
    """Rows from the highest-scored decile down: rate, lift over base rate, and cumulative capture."""
    y = np.asarray(y_true, float)
    s = np.asarray(score, float)
    if len(y) == 0:
        return []
    order = np.argsort(-s, kind="stable")
    y_sorted = y[order]
    base = float(y.mean()) or 1e-12
    total_pos = float(y.sum()) or 1e-12
    chunks = np.array_split(y_sorted, min(n_bins, len(y)))
    out: list[dict[str, float]] = []
    cum = 0.0
    for i, c in enumerate(chunks):
        cum += float(c.sum())
        out.append(
            {
                "decile": float(i + 1),
                "n": float(len(c)),
                "rate": float(c.mean()),
                "lift": float(c.mean()) / base,
                "cum_capture": cum / total_pos,
            }
        )
    return out


def psi_from_proportions(expected: ArrayLike, actual: ArrayLike, eps: float = 1e-4) -> float:
    e = np.clip(np.asarray(expected, float), eps, None)
    a = np.clip(np.asarray(actual, float), eps, None)
    e, a = e / e.sum(), a / a.sum()
    return float(np.sum((a - e) * np.log(a / e)))


def reference_bins(values: ArrayLike, n_bins: int = 10) -> dict[str, list[float]] | None:
    """Quantile bin edges + proportions of a numeric training feature (NaNs form their own bin)."""
    v = np.asarray(values, float)
    finite = v[~np.isnan(v)]
    if len(finite) < 20:
        return None
    edges = np.unique(np.quantile(finite, np.linspace(0, 1, n_bins + 1)[1:-1]))
    props = _bin_props(v, edges)
    return {"edges": [float(e) for e in edges], "props": [float(p) for p in props]}


def _bin_props(values: NDArray[np.float64], edges: NDArray[np.float64]) -> NDArray[np.float64]:
    nan_mask = np.isnan(values)
    idx = np.searchsorted(edges, values[~nan_mask], side="right")
    counts = np.bincount(idx, minlength=len(edges) + 1).astype(float)
    counts = np.append(counts, float(nan_mask.sum()))
    return counts / max(counts.sum(), 1.0)


def psi_against_reference(reference: dict[str, list[float]], values: ArrayLike) -> float:
    v = np.asarray(values, float)
    if len(v) == 0:
        return 0.0
    edges = np.asarray(reference["edges"], float)
    return psi_from_proportions(reference["props"], _bin_props(v, edges))
