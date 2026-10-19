# Performance

Numbers are measurements on an 8-core, 8 GB Apple arm64 laptop (macOS 15.3, Python 3.13.9) that was shared with other heavy jobs
(load average 30-40), 2026-10-19. Full scale analysis, capacity model and the measured/extrapolated table are in [SCALING.md](SCALING.md).

| What | Result | How to reproduce |
|---|---|---|
| SQL ingest, 2M events through connector + upsert | 42.9 s, 46.6k rows/s, peak RSS 107.5 MB (97.8 MB before); re-run in the slow test: 39.5 s, peak 117.2 MB | `make bench` or `uv run pytest -m slow -s tests/perf` |
| Parquet store + profile refresh, 100M events | write 57 s, refresh 11.3 s, peak RSS 638 MB, 1.5 GB disk, 11.3M contacts | `uv run python -m benchmarks.columnar_bench --events 100000000 --chunk 1000000 --segments` |
| same, 300M events | write 224 s, refresh 51.6 s, peak RSS 949 MB, 4.7 GB disk | `--events 300000000` |
| Batch scoring | 6.7k scores/s at 100 rows, 190k/s at 5,000 rows | `uv run python -m benchmarks.scoring_bench` |
| GBM training at the 500k-row cap | 16.5 s, 495 MB peak | `uv run python -m benchmarks.train_bench --rows 500000` |
| Synthetic world sync + lead/SLA/segment training (120 days, 53k events) | about 3 s sync, about 5 s training | `PYTHONPATH=. uv run python` with `tests.conftest.build_env` |
| Test suite | about 4 minutes idle | `uv run pytest` |

Training has a soft budget (`TRAIN_TIME_BUDGET_S`); exceeding it is recorded in the model metrics (`time_budget_exceeded`) rather than aborting.
