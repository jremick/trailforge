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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trailforge import __version__
from trailforge.config import Settings, get_settings
from trailforge.db.models import FeedSyncStatus, TelemetryObservation, TrackSegmentGrade, Trip
from trailforge.db.session import DbHealthStatus, check_db_health, get_engine, reset_engine
from trailforge.feeds.adapters import build_default_adapters
from trailforge.feeds.sync import FeedSyncResult, sync_feed
from trailforge.grading.live import LiveGradingRecomputeSummary, recompute_live_grades
from trailforge.matching.contribution_keys import (
    require_complete_contribution_keyring,
    trip_contribution_hash_sql,
)

logger = logging.getLogger("trailforge.worker")
HEARTBEAT_PATH = Path("/tmp/worker-healthy")
DEFAULT_LIVE_GRADING_INTERVAL_S = 900
DEFAULT_OAUTH_MAINTENANCE_INTERVAL_S = 3600
_last_oauth_maintenance_at: datetime | None = None


def run_due_oauth_maintenance(
    *,
    now: datetime | None = None,
    interval_s: int = DEFAULT_OAUTH_MAINTENANCE_INTERVAL_S,
) -> int:
    """Periodically prune stale unused OAuth DCR clients out of band from the API hot path."""
    global _last_oauth_maintenance_at
    current = now or datetime.now(UTC)
    if (
        _last_oauth_maintenance_at is not None
        and (current - _last_oauth_maintenance_at) < timedelta(seconds=interval_s)
    ):
        return 0

    from trailforge.auth.oauth import get_oauth_unused_client_ttl_days, prune_stale_oauth_clients

    cfg = get_settings()
    engine = get_engine()
    with Session(engine) as session:
        pruned = prune_stale_oauth_clients(
            session,
            now=current,
            unused_ttl_days=get_oauth_unused_client_ttl_days(cfg),
        )
        session.commit()
    _last_oauth_maintenance_at = current
    if pruned > 0:
        logger.info("Pruned %d stale unused OAuth clients", pruned)
    return pruned


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


def run_due_live_grading(
    *,
    now: datetime | None = None,
    interval_s: int | None = None,
    enable_live_rainfall: bool | None = None,
) -> LiveGradingRecomputeSummary | None:
    """Periodically recompute live crowdsourced grades & cached rainfall context (offline-safe).

    Never performs outbound HTTP unless ``TRAILFORGE_ENABLE_LIVE_FEEDS`` is explicitly enabled.
    Skips when there is no shared telemetry or persisted live evidence to refresh.
    """
    current = now or datetime.now(UTC)
    if interval_s is None:
        raw_int = os.environ.get("TRAILFORGE_LIVE_GRADING_INTERVAL_S")
        interval_s = (
            int(raw_int) if raw_int and raw_int.isdigit() else DEFAULT_LIVE_GRADING_INTERVAL_S
        )
    live_rain = (
        enable_live_rainfall
        if enable_live_rainfall is not None
        else _is_truthy(os.environ.get("TRAILFORGE_ENABLE_LIVE_FEEDS"))
    )

    engine = get_engine()
    with Session(engine) as session:
        require_complete_contribution_keyring(session)
        latest_obs = session.scalar(
            select(func.max(TelemetryObservation.created_at))
            .join(Trip, trip_contribution_hash_sql() == TelemetryObservation.contribution_hash)
            .where(
                Trip.visibility == "community_anonymous",
                Trip.community_consent_confirmed.is_(True),
            )
        )
        clear_last_observation = False
        if latest_obs is None:
            clear_last_observation = bool(session.scalar(select(
                select(TrackSegmentGrade.segment_id)
                .where(TrackSegmentGrade.live_grade.is_not(None))
                .exists()
            )))
            if not clear_last_observation and not live_rain:
                return None

        last_computed = session.execute(
            select(func.max(TrackSegmentGrade.live_computed_at))
        ).scalar_one_or_none()
        if last_computed is not None and not clear_last_observation:
            last_dt = (
                last_computed.astimezone(UTC)
                if last_computed.tzinfo
                else last_computed.replace(tzinfo=UTC)
            )
            has_newer_obs = False
            if latest_obs is not None:
                obs_dt = (
                    latest_obs.astimezone(UTC)
                    if latest_obs.tzinfo
                    else latest_obs.replace(tzinfo=UTC)
                )
                has_newer_obs = obs_dt > last_dt
            if not has_newer_obs and (current - last_dt) < timedelta(seconds=interval_s):
                return None

        summary = recompute_live_grades(
            session,
            now=current,
            fetch_live_rainfall=live_rain,
        )
        session.commit()
        logger.info(
            "Recomputed live grades (segments=%d, with_telemetry=%d, hotspots=%d)",
            summary.segments_evaluated,
            summary.segments_with_telemetry,
            summary.obstacle_hotspots_count,
        )
        return summary


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
        try:
            run_due_live_grading()
        except Exception:
            logger.exception("Live grading tick failed unexpectedly; worker remains healthy")
        try:
            run_due_oauth_maintenance()
        except Exception:
            logger.exception("OAuth maintenance tick failed unexpectedly; worker remains healthy")
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
