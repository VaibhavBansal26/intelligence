"""Training time and peak RSS of the GBM pipeline at the per-account row cap: `uv run python -m benchmarks.train_bench --rows 500000`."""

from __future__ import annotations

import argparse
import json
import resource
import time

import numpy as np
import pandas as pd

from app.models.gbm import TrainSpec, train_binary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=500_000)
    n = ap.parse_args().rows
    rng = np.random.default_rng(0)
    f = rng.normal(size=(n, 10))
    logit = 0.9 * f[:, 0] - 0.6 * f[:, 1] + 0.4 * f[:, 2] * f[:, 3] - 1.0
    df = pd.DataFrame(f, columns=[f"x{i}" for i in range(10)])
    df["cat"] = rng.choice(["a", "b", "c", "d"], n)
    df["t"] = np.arange(n) * 30.0
    df["y"] = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(int)
    spec = TrainSpec(
        "bench",
        [f"x{i}" for i in range(10)],
        ["cat"],
        "t",
        "y",
        0.0,
        lambda d, b: np.full(len(d), b),
        {},
        300,
        30,
        1,
        max_rows=500_000,
    )
    t = time.monotonic()
    out = train_binary(df, spec)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
    print(
        json.dumps(
            {
                "rows": n,
                "seconds": round(time.monotonic() - t, 1),
                "auc": out.metrics["auc"],
                "peak_rss_mb": round(rss, 0),
                "features": 11,
            }
        )
    )


if __name__ == "__main__":
    main()
