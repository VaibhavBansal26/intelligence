"""Stand-alone worker process: `uv run python -m app.worker --concurrency 4`.

Run any number of these (and API instances with WORKER_ENABLED=false) against the same database. Claims are atomic
and per-account concurrency is capped, so workers scale out horizontally without coordination.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from app.config import get_settings
from app.deps import build_container
from app.logging import configure_logging

logger = logging.getLogger("intelligence.worker")


async def run(concurrency: int) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    c = build_container(settings)
    runners = [asyncio.create_task(c.runner.run_forever(settings.worker_poll_s)) for _ in range(concurrency)]
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, c.runner.stop)
    logger.info("worker started", extra={"concurrency": concurrency})
    await asyncio.gather(*runners)


def main() -> None:
    ap = argparse.ArgumentParser(description="Intelligence job worker")
    ap.add_argument("--concurrency", type=int, default=2)
    asyncio.run(run(ap.parse_args().concurrency))


if __name__ == "__main__":
    main()
