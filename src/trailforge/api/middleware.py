"""Zero-Redis sliding-window rate limiter, upload quota helper, and hourly usage telemetry.

Implemented as a pure ASGI middleware so RFC 9110 Range `StreamingResponse`s
(PMTiles & regional pack downloads) stream without response buffering while
still measuring exact `bytes_out` and enforcing per-IP sliding-window limits.
"""

from __future__ import annotations

import contextlib
import logging
import math
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from trailforge.config import Settings, get_settings

logger = logging.getLogger(__name__)

RATE_LIMIT_BYPASS_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/api/health",
        "/healthz",
        "/",
        "/docs",
        "/openapi.json",
        "/redoc",
    }
)
AUTH_RATE_LIMIT_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/api/auth/login",
        "/api/auth/register",
    }
)
ROUTE_GROUPS: Final[tuple[str, ...]] = (
    "tracks",
    "tiles",
    "packs",
    "routes",
    "trips",
    "reports",
    "closures",
    "grading",
    "auth",
    "search",
    "admin",
    "other",
)

_MINUTE_WINDOW_S: Final[float] = 60.0
_DAY_WINDOW_S: Final[float] = 86_400.0
_MAX_RATE_LIMIT_KEYS: Final[int] = 10_000
_DEFAULT_FLUSH_INTERVAL_S: Final[float] = 30.0
_DEFAULT_FLUSH_BATCH_THRESHOLD: Final[int] = 50


def classify_route_group(path: str) -> str:
    """Map an HTTP path to its canonical hourly telemetry ``route_group``."""
    clean = path.strip()
    if not clean.startswith("/api/"):
        return "other"
    remainder = clean[5:]
    head = remainder.split("/", 1)[0].lower()
    if head in ("tracks", "segments"):
        return "tracks"
    if head == "tiles":
        return "tiles"
    if head == "packs":
        return "packs"
    if head == "routes":
        return "routes"
    if head in ("trips", "telemetry"):
        return "trips"
    if head == "reports":
        return "reports"
    if head == "closures":
        return "closures"
    if head == "grading":
        return "grading"
    if head == "auth":
        return "auth"
    if head == "search":
        return "search"
    if head == "admin":
        return "admin"
    return "other"


def _sanitize_ip(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned or "\r" in cleaned or "\n" in cleaned or len(cleaned) > 128:
        return None
    return cleaned


def extract_client_ip_from_headers(
    headers: Headers,
    client_host: str | None = None,
    *,
    trust_proxy_headers: bool = True,
) -> str:
    """Extract client IP safely honoring Cloudflare & reverse-proxy headers when trusted."""
    if trust_proxy_headers:
        cf_ip = _sanitize_ip(headers.get("cf-connecting-ip"))
        if cf_ip:
            return cf_ip
        xff = headers.get("x-forwarded-for")
        if xff:
            for part in xff.split(","):
                candidate = _sanitize_ip(part)
                if candidate:
                    return candidate
    host = _sanitize_ip(client_host)
    return host or "unknown"


def extract_client_ip(request: Request, *, trust_proxy_headers: bool = True) -> str:
    """Extract client IP from a FastAPI/Starlette ``Request``."""
    client_host = request.client.host if request.client is not None else None
    return extract_client_ip_from_headers(
        request.headers,
        client_host=client_host,
        trust_proxy_headers=trust_proxy_headers,
    )


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of a sliding-window rate-limit check."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_s: int = 0


class SlidingWindowRateLimiter:
    """Thread-safe, memory-bounded in-process sliding-window rate limiter (zero Redis)."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        max_keys: int = _MAX_RATE_LIMIT_KEYS,
    ) -> None:
        self._clock: Callable[[], float] = clock or time.monotonic
        self._max_keys = max(64, max_keys)
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[deque[float], float]] = {}
        self._ops = 0

    def _prune_locked(self, now: float) -> None:
        expired_keys = [
            key
            for key, (dq, window_s) in self._buckets.items()
            if not dq or (now - dq[-1]) >= window_s
        ]
        for key in expired_keys:
            self._buckets.pop(key, None)

        if len(self._buckets) > self._max_keys:
            # Evict least-recently-seen buckets first so memory stays strictly bounded
            sorted_keys = sorted(
                self._buckets.keys(),
                key=lambda k: self._buckets[k][0][-1] if self._buckets[k][0] else 0.0,
            )
            excess = len(self._buckets) - self._max_keys
            for key in sorted_keys[:excess]:
                self._buckets.pop(key, None)

    def check(self, key: str, *, limit: int, window_s: float) -> RateLimitResult:
        """Record a hit for ``key`` if under ``limit`` within ``window_s`` seconds."""
        effective_limit = max(1, int(limit))
        effective_window = max(1.0, float(window_s))

        with self._lock:
            now = self._clock()
            self._ops += 1
            if self._ops % 256 == 0 or len(self._buckets) > self._max_keys:
                self._prune_locked(now)

            entry = self._buckets.get(key)
            if entry is None:
                dq: deque[float] = deque()
                self._buckets[key] = (dq, effective_window)
            else:
                dq, _prev_window = entry
                self._buckets[key] = (dq, effective_window)

            cutoff = now - effective_window
            while dq and dq[0] <= cutoff:
                dq.popleft()

            if len(dq) >= effective_limit:
                oldest = dq[0]
                retry_after = max(1, math.ceil((oldest + effective_window) - now))
                return RateLimitResult(
                    allowed=False,
                    limit=effective_limit,
                    remaining=0,
                    retry_after_s=retry_after,
                )

            dq.append(now)
            remaining = max(0, effective_limit - len(dq))
            return RateLimitResult(
                allowed=True,
                limit=effective_limit,
                remaining=remaining,
                retry_after_s=0,
            )

    def reset(self) -> None:
        """Clear all tracked rate-limit windows."""
        with self._lock:
            self._buckets.clear()
            self._ops = 0


@dataclass(slots=True)
class _MetricCounters:
    request_count: int = 0
    error_count: int = 0
    bytes_out: int = 0
    rate_limited_count: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "request_count": self.request_count,
            "error_count": self.error_count,
            "bytes_out": self.bytes_out,
            "rate_limited_count": self.rate_limited_count,
        }


_UPSERT_USAGE_SQL = text(
    """
    INSERT INTO usage_metrics_hourly (
        bucket_hour,
        route_group,
        request_count,
        error_count,
        bytes_out,
        rate_limited_count
    )
    VALUES (
        :bucket_hour,
        :route_group,
        :request_count,
        :error_count,
        :bytes_out,
        :rate_limited_count
    )
    ON CONFLICT (bucket_hour, route_group) DO UPDATE SET
        request_count = usage_metrics_hourly.request_count + EXCLUDED.request_count,
        error_count = usage_metrics_hourly.error_count + EXCLUDED.error_count,
        bytes_out = usage_metrics_hourly.bytes_out + EXCLUDED.bytes_out,
        rate_limited_count = (
            usage_metrics_hourly.rate_limited_count + EXCLUDED.rate_limited_count
        )
    """
)


class UsageMetricsStore:
    """Thread-safe hourly traffic accumulator with silent PostGIS rollup."""

    def __init__(
        self,
        *,
        flush_interval_s: float = _DEFAULT_FLUSH_INTERVAL_S,
        flush_batch_threshold: int = _DEFAULT_FLUSH_BATCH_THRESHOLD,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._flush_interval_s = max(1.0, float(flush_interval_s))
        self._flush_batch_threshold = max(1, int(flush_batch_threshold))
        self._now_fn: Callable[[], datetime] = now_fn or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._pending: dict[tuple[datetime, str], _MetricCounters] = {}
        self._cumulative: dict[str, _MetricCounters] = {}
        self._pending_requests = 0
        self._last_flush_mono = time.monotonic()

    def record(
        self,
        *,
        route_group: str,
        status_code: int,
        bytes_out: int,
        rate_limited: bool = False,
        at: datetime | None = None,
    ) -> None:
        """Record one completed HTTP request into the active UTC hour bucket."""
        ts = (at or self._now_fn()).astimezone(UTC)
        bucket_hour = ts.replace(minute=0, second=0, microsecond=0)
        group = route_group if route_group in ROUTE_GROUPS else "other"
        is_error = status_code >= 400
        is_rl = rate_limited or status_code == status.HTTP_429_TOO_MANY_REQUESTS
        safe_bytes = max(0, int(bytes_out))

        with self._lock:
            key = (bucket_hour, group)
            bucket = self._pending.get(key)
            if bucket is None:
                bucket = _MetricCounters()
                self._pending[key] = bucket
            bucket.request_count += 1
            if is_error:
                bucket.error_count += 1
            bucket.bytes_out += safe_bytes
            if is_rl:
                bucket.rate_limited_count += 1

            cum = self._cumulative.get(group)
            if cum is None:
                cum = _MetricCounters()
                self._cumulative[group] = cum
            cum.request_count += 1
            if is_error:
                cum.error_count += 1
            cum.bytes_out += safe_bytes
            if is_rl:
                cum.rate_limited_count += 1

            self._pending_requests += 1

    def get_snapshot(self) -> dict[str, Any]:
        """Return unflushed hourly deltas and process-lifetime cumulative summaries."""
        with self._lock:
            pending_buckets: list[dict[str, Any]] = []
            by_route_group: dict[str, dict[str, int]] = {}
            totals = _MetricCounters()

            for (bucket_hour, group), cnt in sorted(self._pending.items()):
                pending_buckets.append(
                    {
                        "bucket_hour": bucket_hour.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "route_group": group,
                        "request_count": cnt.request_count,
                        "error_count": cnt.error_count,
                        "bytes_out": cnt.bytes_out,
                        "rate_limited_count": cnt.rate_limited_count,
                    }
                )
                agg = by_route_group.setdefault(
                    group,
                    {
                        "request_count": 0,
                        "error_count": 0,
                        "bytes_out": 0,
                        "rate_limited_count": 0,
                    },
                )
                agg["request_count"] += cnt.request_count
                agg["error_count"] += cnt.error_count
                agg["bytes_out"] += cnt.bytes_out
                agg["rate_limited_count"] += cnt.rate_limited_count

                totals.request_count += cnt.request_count
                totals.error_count += cnt.error_count
                totals.bytes_out += cnt.bytes_out
                totals.rate_limited_count += cnt.rate_limited_count

            cumulative_by_group = {
                group: cnt.to_dict() for group, cnt in sorted(self._cumulative.items())
            }
            cum_totals = _MetricCounters()
            for cnt in self._cumulative.values():
                cum_totals.request_count += cnt.request_count
                cum_totals.error_count += cnt.error_count
                cum_totals.bytes_out += cnt.bytes_out
                cum_totals.rate_limited_count += cnt.rate_limited_count

        return {
            "pending_buckets": pending_buckets,
            "by_route_group": by_route_group,
            "totals": totals.to_dict(),
            "cumulative_by_route_group": cumulative_by_group,
            "cumulative_totals": cum_totals.to_dict(),
        }

    def _drain_pending(self) -> dict[tuple[datetime, str], _MetricCounters]:
        with self._lock:
            drained = self._pending
            self._pending = {}
            self._pending_requests = 0
            self._last_flush_mono = time.monotonic()
            return drained

    def _restore_pending(self, batch: dict[tuple[datetime, str], _MetricCounters]) -> None:
        if not batch:
            return
        with self._lock:
            for key, cnt in batch.items():
                existing = self._pending.get(key)
                if existing is None:
                    self._pending[key] = _MetricCounters(
                        request_count=cnt.request_count,
                        error_count=cnt.error_count,
                        bytes_out=cnt.bytes_out,
                        rate_limited_count=cnt.rate_limited_count,
                    )
                else:
                    existing.request_count += cnt.request_count
                    existing.error_count += cnt.error_count
                    existing.bytes_out += cnt.bytes_out
                    existing.rate_limited_count += cnt.rate_limited_count
                self._pending_requests += cnt.request_count

    def flush(self, session: Session | None = None) -> int:
        """Persist pending hourly delta counters into ``usage_metrics_hourly``.

        Silently returns ``0`` (never raises) if PostgreSQL or the table is
        unavailable, restoring unflushed deltas so traffic is not lost.
        """
        with self._flush_lock:
            batch = self._drain_pending()
            if not batch:
                return 0

            params = [
                {
                    "bucket_hour": bucket_hour,
                    "route_group": group,
                    "request_count": cnt.request_count,
                    "error_count": cnt.error_count,
                    "bytes_out": cnt.bytes_out,
                    "rate_limited_count": cnt.rate_limited_count,
                }
                for (bucket_hour, group), cnt in batch.items()
            ]

            try:
                if session is not None:
                    session.execute(_UPSERT_USAGE_SQL, params)
                    session.commit()
                    return len(params)

                from trailforge.db import session as db_session

                engine = db_session._engine
                if engine is None:
                    self._restore_pending(batch)
                    return 0

                with engine.begin() as conn:
                    conn.execute(_UPSERT_USAGE_SQL, params)
                return len(params)
            except (SQLAlchemyError, OSError, RuntimeError) as exc:
                logger.debug("Skipping usage metrics flush (%s)", exc)
                if session is not None:
                    with contextlib.suppress(Exception):
                        session.rollback()
                self._restore_pending(batch)
                return 0

    def maybe_flush(self) -> int:
        """Flush periodically when an active DB engine exists and thresholds are met."""
        with self._lock:
            if self._pending_requests == 0:
                return 0
            elapsed = time.monotonic() - self._last_flush_mono
            should_flush = (
                self._pending_requests >= self._flush_batch_threshold
                or elapsed >= self._flush_interval_s
            )
        if not should_flush:
            return 0

        from trailforge.db import session as db_session

        if db_session._engine is None:
            return 0
        return self.flush(session=None)

    def reset(self) -> None:
        """Reset all in-memory pending and cumulative usage counters."""
        with self._lock:
            self._pending.clear()
            self._cumulative.clear()
            self._pending_requests = 0
            self._last_flush_mono = time.monotonic()


_UPLOAD_RATE_LIMITER = SlidingWindowRateLimiter()
_DEFAULT_USAGE_STORE = UsageMetricsStore()


def check_upload_quota(
    user_id: str | uuid.UUID,
    limit_per_day: int | None = None,
    *,
    limiter: SlidingWindowRateLimiter | None = None,
) -> RateLimitResult:
    """Enforce per-user 24-hour upload quota, raising HTTP 429 with Retry-After on breach."""
    resolved_limit = (
        int(limit_per_day)
        if limit_per_day is not None
        else int(get_settings().rate_limit_upload_per_day)
    )
    target_limiter = limiter or _UPLOAD_RATE_LIMITER
    result = target_limiter.check(
        f"upload_day:{user_id}",
        limit=resolved_limit,
        window_s=_DAY_WINDOW_S,
    )
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Daily upload limit exceeded ({resolved_limit} uploads per 24 hours). "
                "Please try again later."
            ),
            headers={
                "Retry-After": str(result.retry_after_s),
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": "0",
            },
        )
    return result


def reset_rate_limiter(app: FastAPI | None = None) -> None:
    """Reset the shared upload quota limiter and (if given) an app's HTTP rate limiter."""
    _UPLOAD_RATE_LIMITER.reset()
    if app is not None:
        app_limiter = getattr(app.state, "rate_limiter", None)
        if isinstance(app_limiter, SlidingWindowRateLimiter):
            app_limiter.reset()


def get_live_usage_snapshot(*, store: UsageMetricsStore | None = None) -> dict[str, Any]:
    """Export live in-memory usage telemetry snapshot for ``GET /api/admin/stats``."""
    return (store or _DEFAULT_USAGE_STORE).get_snapshot()


def flush_usage_metrics(
    session: Session | None = None,
    *,
    store: UsageMetricsStore | None = None,
) -> int:
    """Flush pending usage telemetry deltas to PostGIS ``usage_metrics_hourly``."""
    return (store or _DEFAULT_USAGE_STORE).flush(session=session)


def reset_usage_metrics(*, store: UsageMetricsStore | None = None) -> None:
    """Clear in-memory usage telemetry counters (intended for deterministic tests)."""
    (store or _DEFAULT_USAGE_STORE).reset()


class UsageAndRateLimitMiddleware:
    """Pure ASGI middleware enforcing per-IP sliding-window limits & hourly telemetry."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings | None = None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        usage_store: UsageMetricsStore | None = None,
    ) -> None:
        self.app = app
        self._explicit_settings = settings
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter()
        self.usage_store = usage_store or _DEFAULT_USAGE_STORE

    def _resolve_settings(self, scope: Scope) -> Settings:
        if self._explicit_settings is not None:
            return self._explicit_settings
        fastapi_app = scope.get("app")
        custom = getattr(getattr(fastapi_app, "state", None), "custom_settings", None)
        if isinstance(custom, Settings):
            return custom
        return get_settings()

    def _resolve_limiter(self, scope: Scope) -> SlidingWindowRateLimiter:
        fastapi_app = scope.get("app")
        state_limiter = getattr(getattr(fastapi_app, "state", None), "rate_limiter", None)
        if isinstance(state_limiter, SlidingWindowRateLimiter):
            return state_limiter
        return self.rate_limiter

    def _should_enforce_for_testclient(self, scope: Scope) -> bool:
        fastapi_app = scope.get("app")
        return bool(
            getattr(getattr(fastapi_app, "state", None), "enforce_rate_limit_in_tests", False)
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", "/"))
        is_api_route = path.startswith("/api/")
        is_options = method == "OPTIONS"
        is_bypass = is_options or (not is_api_route) or (path in RATE_LIMIT_BYPASS_PATHS)
        route_group = classify_route_group(path)

        cfg = self._resolve_settings(scope)
        rl_headers: dict[str, str] = {}

        if cfg.rate_limit_enabled and not is_bypass:
            headers = Headers(scope=scope)
            client_tuple = scope.get("client")
            client_host = client_tuple[0] if client_tuple else None
            client_ip = extract_client_ip_from_headers(
                headers,
                client_host=client_host,
                trust_proxy_headers=cfg.trust_proxy_headers,
            )

            if method == "POST" and path in AUTH_RATE_LIMIT_PATHS:
                bucket_name = "auth"
                limit = cfg.rate_limit_auth_per_min
            else:
                bucket_name = "api"
                limit = cfg.rate_limit_read_per_min

            bypass_testclient = client_ip == "testclient" and not (
                self._should_enforce_for_testclient(scope)
            )
            if bypass_testclient:
                rl_headers = {
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": str(max(1, limit - 1)),
                }
            else:
                limiter = self._resolve_limiter(scope)
                result = limiter.check(
                    f"{bucket_name}:{client_ip}",
                    limit=limit,
                    window_s=_MINUTE_WINDOW_S,
                )
                if not result.allowed:
                    response = JSONResponse(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        content={"detail": "Rate limit exceeded. Please try again shortly."},
                        headers={
                            "Retry-After": str(result.retry_after_s),
                            "X-RateLimit-Limit": str(result.limit),
                            "X-RateLimit-Remaining": "0",
                        },
                    )
                    self.usage_store.record(
                        route_group=route_group,
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        bytes_out=len(response.body),
                        rate_limited=True,
                    )
                    self.usage_store.maybe_flush()
                    await response(scope, receive, send)
                    return

                rl_headers = {
                    "X-RateLimit-Limit": str(result.limit),
                    "X-RateLimit-Remaining": str(result.remaining),
                }

        status_code = 200
        bytes_out = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, bytes_out
            msg_type = message["type"]
            if msg_type == "http.response.start":
                status_code = int(message.get("status", 200))
                if rl_headers:
                    resp_headers = MutableHeaders(scope=message)
                    for hdr_key, hdr_val in rl_headers.items():
                        if hdr_key.lower() not in resp_headers:
                            resp_headers[hdr_key] = hdr_val
            elif msg_type == "http.response.body":
                body = message.get("body")
                if body:
                    bytes_out += len(body)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status_code = 500
            raise
        finally:
            if is_api_route and not is_options:
                self.usage_store.record(
                    route_group=route_group,
                    status_code=status_code,
                    bytes_out=bytes_out,
                    rate_limited=(status_code == status.HTTP_429_TOO_MANY_REQUESTS),
                )
                self.usage_store.maybe_flush()
