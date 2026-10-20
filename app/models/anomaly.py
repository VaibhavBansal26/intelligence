"""Anomaly and trend detection on regular time series.

Baseline: seasonal-naive MEDIAN of the same phase over the previous K periods (weekday medians for daily
data, hour-of-week for hourly). Medians keep earlier anomalies from contaminating the baseline.
Detectors on the robust residuals: generalised ESD (point outliers, MAD-scaled) and two-sided CUSUM
(sustained shifts). Guard rails keep false positives low: minimum history, minimum relative AND absolute
deviation, floor on the noise scale (Poisson-like for counts), and run-length requirements. Consecutive
flags merge into one episode with a stable fingerprint so repeated runs do not re-alert.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
from numpy.typing import NDArray
from scipy import stats


@dataclass
class DetectorConfig:
    period: int = 7
    n_periods: int = 6
    min_history_periods: int = 3
    esd_alpha: float = 0.01
    max_outliers: int = 8
    cusum_k: float = 0.75
    cusum_h: float = 8.0
    cusum_min_run: int = 3
    min_rel_dev: float = 0.25
    min_abs_dev: float = 5.0
    count_like: bool = True
    z_floor: float = 3.5  # a point must also have |z| >= z_floor
    # collapse rule (count-like series): a run of days far below the seasonal baseline is reported even when the series is
    # so noisy that the z-based detectors cannot reach their thresholds. Found on real app traffic: a funnel that normally
    # gets about 50 page views a day (daily CV ~25%) lost 70% of them for three days and neither ESD nor CUSUM fired.
    collapse_ratio: float = 0.4
    collapse_min_run: int = 3
    collapse_min_baseline: float = 10.0


@dataclass
class Episode:
    start_idx: int
    end_idx: int
    kind: str  # point | shift
    direction: str  # drop | spike
    severity: float
    observed: float
    expected: float
    detectors: list[str] = field(default_factory=list)

    def fingerprint(self, metric: str, scope: str, times: NDArray[np.int64]) -> str:
        day = datetime.fromtimestamp(int(times[self.start_idx]), UTC).strftime("%Y-%m-%d")
        return hashlib.sha256(f"{metric}|{scope}|{self.direction}|{day}".encode()).hexdigest()[:32]


@dataclass
class DetectionResult:
    episodes: list[Episode]
    status: str  # ok | insufficient_history
    n_points: int
    residual_scale: float | None = None


def seasonal_baseline(x: NDArray[np.float64], period: int, n_periods: int) -> NDArray[np.float64]:
    n = len(x)
    base = np.full(n, np.nan)
    for t in range(period, n):
        k = min(n_periods, t // period)
        lags = x[[t - j * period for j in range(1, k + 1)]]
        lags = lags[~np.isnan(lags)]
        if len(lags) >= 3:
            base[t] = float(np.median(lags))
    return base


def _mad_scale(v: NDArray[np.float64]) -> float:
    v = v[~np.isnan(v)]
    if len(v) == 0:
        return 0.0
    return float(1.4826 * np.median(np.abs(v - np.median(v))))


def generalized_esd(z: NDArray[np.float64], alpha: float, max_outliers: int) -> set[int]:
    """Rosner's generalised ESD on already robust-standardised values; returns indices of outliers."""
    idx = np.flatnonzero(~np.isnan(z))
    vals = z[idx].copy()
    n = len(vals)
    if n < 10:
        return set()
    removed: list[int] = []
    stat: list[float] = []
    work = vals.copy()
    alive = np.ones(n, bool)
    for _ in range(min(max_outliers, n // 3)):
        cur = work[alive]
        centre = float(np.median(cur))
        scale = _mad_scale(cur) or float(np.std(cur)) or 1.0
        dev = np.abs(work - centre) / scale
        dev[~alive] = -1
        j = int(np.argmax(dev))
        stat.append(float(dev[j]))
        removed.append(j)
        alive[j] = False
    n_out = 0
    for i, r in enumerate(stat, 1):
        m = n - i + 1
        p = 1 - alpha / (2 * m)
        tcrit = stats.t.ppf(p, m - 2)
        lam = (m - 1) * tcrit / np.sqrt((m - 2 + tcrit**2) * m)
        if r > lam:
            n_out = i
    return {int(idx[j]) for j in removed[:n_out]}


def cusum_runs(z: NDArray[np.float64], k: float, h: float) -> list[tuple[int, int, str]]:
    """Two-sided CUSUM; yields (start, end, direction) of each alarm run (reset after each alarm)."""
    out: list[tuple[int, int, str]] = []
    sp = sn = 0.0
    sp_start = sn_start = 0
    for i, v in enumerate(z):
        if np.isnan(v):
            continue
        if sp == 0.0:
            sp_start = i
        if sn == 0.0:
            sn_start = i
        sp = max(0.0, sp + v - k)
        sn = max(0.0, sn - v - k)
        if sp > h:
            out.append((sp_start, i, "spike"))
            sp = sn = 0.0
        elif sn > h:
            out.append((sn_start, i, "drop"))
            sp = sn = 0.0
    return out


def detect(values: NDArray[np.float64], cfg: DetectorConfig | None = None) -> DetectionResult:
    cfg = cfg or DetectorConfig()
    x = np.asarray(values, float)
    n = len(x)
    if n < cfg.period * (cfg.min_history_periods + 1) or np.isnan(x).mean() > 0.5:
        return DetectionResult([], "insufficient_history", n)
    base = seasonal_baseline(x, cfg.period, cfg.n_periods)
    resid = x - base
    scale = _mad_scale(resid)
    floor = (
        0.5 * np.sqrt(np.maximum(np.nan_to_num(base, nan=1.0), 1.0))
        if cfg.count_like
        else 0.03 * np.abs(np.nan_to_num(base, nan=0.0))
    )
    s_t = np.maximum(scale, np.maximum(floor, 1e-9))
    z = resid / s_t
    rel_ok = np.abs(resid) >= cfg.min_rel_dev * np.maximum(np.abs(np.nan_to_num(base, nan=0.0)), 1e-9)
    abs_ok = np.abs(resid) >= cfg.min_abs_dev if cfg.count_like else np.ones(n, bool)
    esd = generalized_esd(z, cfg.esd_alpha, cfg.max_outliers)
    flagged: dict[int, str] = {}
    for i in esd:
        if abs(z[i]) >= cfg.z_floor and rel_ok[i] and abs_ok[i]:
            flagged[i] = "esd"
    episodes: list[Episode] = []
    # CUSUM shifts on the clipped standardised residual (clipping stops a single outlier from alarming alone)
    zc = np.clip(np.where(np.isnan(z), 0.0, z), -4.0, 4.0)
    for s, e, direction in cusum_runs(zc, cfg.cusum_k, cfg.cusum_h):
        if e - s + 1 < cfg.cusum_min_run:
            continue
        seg = slice(s, e + 1)
        rel = np.nanmean(resid[seg]) / max(abs(float(np.nanmean(base[seg]))), 1e-9)
        if abs(rel) < cfg.min_rel_dev or (cfg.count_like and abs(float(np.nanmean(resid[seg]))) < cfg.min_abs_dev):
            continue
        episodes.append(
            Episode(
                s,
                e,
                "shift",
                direction,
                float(np.nanmax(np.abs(z[seg]))),
                float(np.nanmean(x[seg])),
                float(np.nanmean(base[seg])),
                ["cusum"],
            )
        )
    if cfg.count_like and cfg.collapse_min_run > 0:
        episodes.extend(_collapse_runs(x, base, z, cfg))
    for i in sorted(flagged):
        direction = "spike" if resid[i] > 0 else "drop"
        episodes.append(Episode(i, i, "point", direction, float(abs(z[i])), float(x[i]), float(base[i]), ["esd"]))
    return DetectionResult(_merge(episodes, x, base), "ok", n, float(scale))


def _collapse_runs(
    x: NDArray[np.float64], base: NDArray[np.float64], z: NDArray[np.float64], cfg: DetectorConfig
) -> list[Episode]:
    """Runs of at least `collapse_min_run` consecutive days at or below `collapse_ratio` x baseline (baseline big enough to matter).
    With daily noise of 25-30% a single day under 40% of expected happens about once in 70 days, three in a row about once in 300,000."""
    b = np.nan_to_num(base, nan=0.0)
    low = (b >= cfg.collapse_min_baseline) & (x <= cfg.collapse_ratio * b)
    out: list[Episode] = []
    i, n = 0, len(x)
    while i < n:
        if not low[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and low[j + 1]:
            j += 1
        if j - i + 1 >= cfg.collapse_min_run:
            seg = slice(i, j + 1)
            sev = float(np.nanmax(np.abs(z[seg]))) if not np.all(np.isnan(z[seg])) else 0.0
            out.append(Episode(i, j, "shift", "drop", sev, float(np.nanmean(x[seg])), float(np.nanmean(base[seg])), ["collapse"]))
        i = j + 1
    return out


def _merge(episodes: list[Episode], x: NDArray[np.float64], base: NDArray[np.float64]) -> list[Episode]:
    """Merge overlapping/adjacent same-direction episodes into one (dedupe across detectors and consecutive days)."""
    merged: list[Episode] = []
    for ep in sorted(episodes, key=lambda e: (e.direction, e.start_idx)):
        last = merged[-1] if merged else None
        if last and last.direction == ep.direction and ep.start_idx <= last.end_idx + 2:
            last.end_idx = max(last.end_idx, ep.end_idx)
            last.start_idx = min(last.start_idx, ep.start_idx)
            last.severity = max(last.severity, ep.severity)
            last.detectors = sorted(set(last.detectors) | set(ep.detectors))
            last.kind = "shift" if last.end_idx > last.start_idx or "cusum" in last.detectors else "point"
            seg = slice(last.start_idx, last.end_idx + 1)
            last.observed, last.expected = float(np.nanmean(x[seg])), float(np.nanmean(base[seg]))
        else:
            merged.append(ep)
    return sorted(merged, key=lambda e: e.start_idx)
