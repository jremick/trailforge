"""Lightweight async/polling background worker for TrailForge.

Runs without Redis or Kafka to honor homelab and 16 GB laptop guardrails.
Future slices register periodic jobs (VIC/NSW closure feed sync, telemetry
aggregation, rainfall snapshots, and regional PMTiles pack builds).
"""

from __future__ import annotations

import logging
import signal
import threading
from pathlib import Path
from types import FrameType

from trailforge import __version__
from trailforge.config import Settings, get_settings
from trailforge.db.session import DbHealthStatus, check_db_health, reset_engine

logger = logging.getLogger("trailforge.worker")
HEARTBEAT_PATH = Path("/tmp/worker-healthy")


def touch_heartbeat(path: Path = HEARTBEAT_PATH) -> None:
    """Record worker liveness timestamp for Docker healthcheck inspection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok\n", encoding="utf-8")


def run_worker_tick(heartbeat_path: Path = HEARTBEAT_PATH) -> DbHealthStatus:
    """Execute one lightweight worker poll cycle and update the liveness file when DB is ready."""
    status = check_db_health()
    if status.db_ok:
        touch_heartbeat(heartbeat_path)
        logger.debug(
            "Worker heartbeat ok (postgis=%s, schema=%s)",
            status.postgis_version,
            status.schema_revision,
        )
    else:
        logger.warning("Worker DB health check failed; skipping scheduled jobs this cycle")
    return status


def run(settings: Settings | None = None) -> None:
    """Run the polling worker loop until SIGINT or SIGTERM is received."""
    cfg = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received signal %s; shutting down worker cleanly", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "TrailForge worker v%s started (poll_seconds=%d)",
        __version__,
        cfg.worker_poll_seconds,
    )

    try:
        while not stop_event.is_set():
            run_worker_tick()
            stop_event.wait(timeout=cfg.worker_poll_seconds)
    finally:
        reset_engine()
        logger.info("TrailForge worker stopped")


if __name__ == "__main__":
    run()
