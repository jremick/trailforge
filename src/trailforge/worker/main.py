"""Lightweight async/polling background worker for TrailForge.

Runs without Redis or Kafka to honor homelab and 16 GB laptop guardrails.
Periodically runs scheduled jobs (VIC/NSW closure feed syncs when enabled, plus future
telemetry aggregation, rainfall snapshots, and regional PMTiles pack builds).
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType

from sqlalchemy import select
from sqlalchemy.orm import Session

from trailforge import __version__
from trailforge.config import Settings, get_settings
from trailforge.db.models import FeedSyncStatus
from trailforge.db.session import DbHealthStatus, check_db_health, get_engine, reset_engine
from trailforge.feeds.adapters import build_default_adapters
from trailforge.feeds.sync import FeedSyncResult, sync_feed

logger = logging.getLogger("trailforge.worker")
HEARTBEAT_PATH = Path("/tmp/worker-healthy")


def touch_heartbeat(path: Path = HEARTBEAT_PATH) -> None:
    """Record worker liveness timestamp for Docker healthcheck inspection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok\n", encoding="utf-8")


def _is_truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def run_due_closure_syncs(
    *,
    now: datetime | None = None,
    enable_live_feeds: bool | None = None,
    fixtures_path: Path | str | None = None,
) -> list[FeedSyncResult]:
    """Run any due closure feed adapters based on ``feed_sync_status.sync_interval_s``.

    Offline-safe by default: external HTTP feeds only run when ``TRAILFORGE_ENABLE_LIVE_FEEDS``
    is explicitly enabled; when ``TRAILFORGE_CLOSURES_FIXTURES_PATH`` is provided, offline
    fixture sync runs cleanly without any network calls.
    """
    live_enabled = (
        enable_live_feeds
        if enable_live_feeds is not None
        else _is_truthy(os.environ.get("TRAILFORGE_ENABLE_LIVE_FEEDS"))
    )
    resolved_fixtures = (
        fixtures_path
        if fixtures_path is not None
        else os.environ.get("TRAILFORGE_CLOSURES_FIXTURES_PATH")
    )

    if not live_enabled and not resolved_fixtures:
        return []

    current = now or datetime.now(UTC)
    adapters = build_default_adapters(
        fixtures_path=resolved_fixtures if (resolved_fixtures and not live_enabled) else None
    )

    results: list[FeedSyncResult] = []
    engine = get_engine()
    with Session(engine) as session:
        existing_status = {
            row.feed_name: row
            for row in session.execute(select(FeedSyncStatus)).scalars().all()
        }
        for adapter in adapters:
            row = existing_status.get(adapter.feed_source)
            is_due = row is None or row.last_attempt_at is None
            if not is_due and row is not None and row.last_attempt_at is not None:
                last_try = (
                    row.last_attempt_at.astimezone(UTC)
                    if row.last_attempt_at.tzinfo
                    else row.last_attempt_at.replace(tzinfo=UTC)
                )
                is_due = (current - last_try) >= timedelta(seconds=adapter.sync_interval_s)

            if not is_due:
                continue

            res = sync_feed(session, adapter, now=current, commit=True)
            results.append(res)
            if res.ok:
                logger.info(
                    "Synced closure feed %s (upserted=%d, linked=%d)",
                    res.feed_source,
                    res.records_upserted,
                    res.segments_linked,
                )
            else:
                logger.warning(
                    "Closure feed %s degraded (retained=%d): %s",
                    res.feed_source,
                    res.retained_existing,
                    res.error,
                )
    return results


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
        try:
            run_due_closure_syncs()
        except Exception:
            logger.exception("Closure sync tick failed unexpectedly; worker remains healthy")
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
