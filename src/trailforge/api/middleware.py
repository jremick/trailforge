"""Zero-Redis edge security, sliding-window rate limiter, quotas, and hourly usage telemetry.

Implemented as pure ASGI middlewares so RFC 9110 Range `StreamingResponse`s
(PMTiles & regional pack downloads) stream without response buffering while
enforcing origin authentication, streaming body-size caps, cookie CSRF checks,
global security response headers, and per-IP sliding-window limits.
"""

from __future__ import annotations

import contextlib
import hmac
import ipaddress
import logging
import math
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from trailforge.config import Settings, get_settings
from trailforge.mcp.context import extract_bare_origin

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
ORIGIN_SECRET_HEADER: Final[str] = "x-trailforge-origin-secret"
ORIGIN_SECRET_BYPASS_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/api/health",
        "/healthz",
    }
)
AUTH_RATE_LIMIT_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/api/auth/login",
        "/api/auth/register",
        "/api/oauth/authorize",
        "/api/oauth/token",
        "/api/oauth/revoke",
    }
)
OAUTH_DCR_PATH: Final[str] = "/api/oauth/register"
MAX_BODY_TRIPS_BYTES: Final[int] = 16 * 1024 * 1024  # 16 MiB on /api/trips/*
MAX_BODY_REPORTS_BYTES: Final[int] = 6 * 1024 * 1024  # 6 MiB on /api/reports*
MAX_BODY_API_DEFAULT_BYTES: Final[int] = 1 * 1024 * 1024  # 1 MiB on /api/*, /mcp*, /api/oauth/*

MCP_CORS_ALLOWED_HEADERS: Final[str] = (
    "Authorization, Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version, Last-Event-ID"
)
MCP_CORS_EXPOSED_HEADERS: Final[str] = (
    "Mcp-Session-Id, WWW-Authenticate, X-RateLimit-Limit, X-RateLimit-Remaining"
)

MUTATING_HTTP_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ALLOWED_SEC_FETCH_SITES: Final[frozenset[str]] = frozenset({"same-origin", "none"})

SECURITY_RESPONSE_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(self)"),
)
HSTS_HEADER_VALUE: Final[str] = "max-age=31536000; includeSubDomains"
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
    "mcp",
    "search",
    "admin",
    "other",
)

_MINUTE_WINDOW_S: Final[float] = 60.0
_DAY_WINDOW_S: Final[float] = 86_400.0
_MAX_RATE_LIMIT_KEYS: Final[int] = 10_000
_DEFAULT_FLUSH_INTERVAL_S: Final[float] = 30.0
_DEFAULT_FLUSH_BATCH_THRESHOLD: Final[int] = 50


def is_mcp_path(path: str) -> bool:
    """Return True if ``path`` targets ``/mcp`` or ``/mcp/*``."""
    normalized = path.strip().rstrip("/") or "/"
    return normalized == "/mcp" or normalized.startswith("/mcp/")


def is_oauth_well_known_path(path: str) -> bool:
    """Return True if ``path`` targets an RFC 9728 / RFC 8414 ``/.well-known/oauth-*`` endpoint."""
    normalized = path.strip().rstrip("/") or "/"
    return normalized.startswith("/.well-known/oauth-")


def classify_route_group(path: str) -> str:
    """Map an HTTP path to its canonical hourly telemetry ``route_group``."""
    clean = path.strip()
    normalized = clean.rstrip("/") or "/"
    if normalized == "/mcp" or normalized.startswith("/mcp/"):
        return "mcp"
    if normalized.startswith("/.well-known/oauth-"):
        return "auth"
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
    if head in ("auth", "oauth"):
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


def _validate_proxy_ip(value: str | None) -> str | None:
    """Validate and canonicalize a header-supplied IP address; reject non-IP strings."""
    cleaned = _sanitize_ip(value)
    if not cleaned:
        return None
    try:
        return str(ipaddress.ip_address(cleaned))
    except ValueError:
        return None


def extract_client_ip_from_headers(
    headers: Headers,
    client_host: str | None = None,
    *,
    trust_proxy_headers: bool = True,
) -> str:
    """Extract client IP safely honoring validated Cloudflare & reverse-proxy headers."""
    if trust_proxy_headers:
        if "cf-connecting-ip" in headers:
            cf_ip = _validate_proxy_ip(headers.get("cf-connecting-ip"))
            if cf_ip:
                return cf_ip
        elif "x-forwarded-for" in headers:
            xff = headers.get("x-forwarded-for") or ""
            for part in xff.split(","):
                if part.strip():
                    candidate = _validate_proxy_ip(part)
                    if candidate:
                        return candidate
                    break
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
    """Thread-safe, O(1)-LRU memory-bounded in-process sliding-window rate limiter."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        max_keys: int = _MAX_RATE_LIMIT_KEYS,
    ) -> None:
        self._clock: Callable[[], float] = clock or time.monotonic
        self._max_keys = max(64, max_keys)
        self._lock = threading.Lock()
        self._buckets: OrderedDict[str, tuple[deque[float], float]] = OrderedDict()

    def _evict_expired_head_locked(self, now: float, *, skip_key: str) -> None:
        """Pop already-expired LRU entries from the front of the OrderedDict in O(1) amortized."""
        while self._buckets:
            oldest_key, (oldest_dq, oldest_window) = next(iter(self._buckets.items()))
            if oldest_key == skip_key:
                break
            if not oldest_dq or (now - oldest_dq[-1]) >= oldest_window:
                self._buckets.popitem(last=False)
            else:
                break

    def check(self, key: str, *, limit: int, window_s: float) -> RateLimitResult:
        """Record a hit for ``key`` if under ``limit`` within ``window_s`` seconds."""
        effective_limit = max(1, int(limit))
        effective_window = max(1.0, float(window_s))

        with self._lock:
            now = self._clock()
            self._evict_expired_head_locked(now, skip_key=key)

            entry = self._buckets.get(key)
            if entry is None:
                # O(1) LRU eviction keeps memory strictly bounded even under spoofed IP floods
                while len(self._buckets) >= self._max_keys:
                    self._buckets.popitem(last=False)
                dq: deque[float] = deque()
                self._buckets[key] = (dq, effective_window)
            else:
                dq, _prev_window = entry
                self._buckets[key] = (dq, effective_window)
                self._buckets.move_to_end(key)

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


def _resolve_settings_from_scope(scope: Scope, explicit: Settings | None) -> Settings:
    if explicit is not None:
        return explicit
    fastapi_app = scope.get("app")
    custom = getattr(getattr(fastapi_app, "state", None), "custom_settings", None)
    if isinstance(custom, Settings):
        return custom
    return get_settings()


def resolve_body_size_limit(
    path: str,
    *,
    trips_max_bytes: int = MAX_BODY_TRIPS_BYTES,
    reports_max_bytes: int = MAX_BODY_REPORTS_BYTES,
    default_api_max_bytes: int = MAX_BODY_API_DEFAULT_BYTES,
) -> int | None:
    """Return the maximum permitted request body size in bytes for ``path``."""
    normalized = path.rstrip("/") or "/"
    if is_mcp_path(normalized):
        return default_api_max_bytes
    if normalized != "/api" and not normalized.startswith("/api/"):
        return None
    if normalized == "/api/trips" or normalized.startswith("/api/trips/"):
        return trips_max_bytes
    if normalized == "/api/reports" or normalized.startswith("/api/reports/"):
        return reports_max_bytes
    return default_api_max_bytes


class _PayloadTooLargeError(Exception):
    """Internal sentinel raised by wrapped ASGI receive() when streamed bytes exceed limit."""

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"Request body exceeds {max_bytes} bytes")
        self.max_bytes = max_bytes


class BodySizeLimitMiddleware:
    """Pure ASGI streaming body-size limiter rejecting oversized payloads with HTTP 413.

    Enforces both ``Content-Length`` fast-fail and incremental ASGI ``receive``
    chunk counting *before* FastAPI parses JSON or spools multipart form bodies.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        trips_max_bytes: int = MAX_BODY_TRIPS_BYTES,
        reports_max_bytes: int = MAX_BODY_REPORTS_BYTES,
        default_api_max_bytes: int = MAX_BODY_API_DEFAULT_BYTES,
    ) -> None:
        self.app = app
        self._trips_max_bytes = max(1, int(trips_max_bytes))
        self._reports_max_bytes = max(1, int(reports_max_bytes))
        self._default_api_max_bytes = max(1, int(default_api_max_bytes))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", "/"))
        max_bytes = resolve_body_size_limit(
            path,
            trips_max_bytes=self._trips_max_bytes,
            reports_max_bytes=self._reports_max_bytes,
            default_api_max_bytes=self._default_api_max_bytes,
        )
        if max_bytes is None:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_length_raw = headers.get("content-length")
        if content_length_raw is not None:
            try:
                content_length = int(content_length_raw.strip())
                if content_length < 0:
                    raise ValueError("negative")
            except ValueError:
                bad_req = JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Invalid Content-Length header."},
                )
                await bad_req(scope, receive, send)
                return

            if content_length > max_bytes:
                too_large = JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={
                        "detail": (
                            f"Payload Too Large: request body exceeds maximum allowed size "
                            f"({max_bytes} bytes)."
                        )
                    },
                )
                await too_large(scope, receive, send)
                return

        received_bytes = 0
        payload_too_large = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received_bytes, payload_too_large
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body") or b""
                received_bytes += len(chunk)
                if received_bytes > max_bytes:
                    payload_too_large = True
                    raise _PayloadTooLargeError(max_bytes)
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            # If receive() already tripped the byte cap, suppress any downstream
            # 400/500 error that FastAPI/Starlette generated by catching the exception
            # so we emit a clean HTTP 413 Payload Too Large response instead.
            if payload_too_large and not response_started:
                return
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _PayloadTooLargeError:
            if response_started:
                raise

        if payload_too_large and not response_started:
            too_large = JSONResponse(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                content={
                    "detail": (
                        f"Payload Too Large: request body exceeds maximum allowed size "
                        f"({max_bytes} bytes)."
                    )
                },
            )
            await too_large(scope, receive, send)


def _has_bearer_authorization(headers: Headers) -> bool:
    auth = (headers.get("authorization") or "").strip()
    scheme, sep, token = auth.partition(" ")
    return bool(sep and scheme.lower() == "bearer" and token.strip())


def _has_cookie(headers: Headers, cookie_name: str) -> bool:
    for header_val in headers.getlist("cookie"):
        for part in header_val.split(";"):
            name, sep, val = part.partition("=")
            if sep == "=" and name.strip() == cookie_name and bool(val.strip()):
                return True
    return False


def _allowed_csrf_origins(scope: Scope, headers: Headers, cfg: Settings) -> set[str]:
    allowed: set[str] = set()
    for origin in cfg.cors_origins_list:
        cleaned = origin.strip().rstrip("/").lower()
        if cleaned and cleaned != "*":
            allowed.add(cleaned)

    scheme = str(scope.get("scheme", "http")).strip().lower() or "http"
    host = (headers.get("host") or "").strip().lower()
    if host:
        allowed.add(f"{scheme}://{host}")

    if cfg.trust_proxy_headers:
        xf_proto = (headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
        xf_host = (headers.get("x-forwarded-host") or "").split(",", 1)[0].strip().lower()
        effective_proto = xf_proto or scheme
        effective_host = xf_host or host
        if effective_proto and effective_host:
            allowed.add(f"{effective_proto}://{effective_host}")

    return allowed


class CookieCSRFMiddleware:
    """Pure ASGI CSRF guard for cookie-authenticated mutating HTTP requests.

    On mutating requests (POST/PUT/PATCH/DELETE) carrying the ``tf_session`` cookie
    without an ``Authorization: Bearer`` header:
    - If ``Sec-Fetch-Site`` is present, requires it to be ``same-origin`` or ``none``.
    - Else if ``Origin`` is present, requires it to match the request origin or an
      allowlisted entry in ``CORS_ORIGINS``.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings | None = None) -> None:
        self.app = app
        self._explicit_settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        if method not in MUTATING_HTTP_METHODS:
            await self.app(scope, receive, send)
            return

        # /mcp and /mcp/* are strictly Bearer-only (never read cookies), so cross-origin MCP
        # clients are never blocked by cookie CSRF checks while /api/* (incl. /api/oauth/authorize)
        # retains full cookie CSRF enforcement.
        path = str(scope.get("path", "/"))
        if is_mcp_path(path):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if _has_bearer_authorization(headers):
            await self.app(scope, receive, send)
            return

        cfg = _resolve_settings_from_scope(scope, self._explicit_settings)
        if not _has_cookie(headers, cfg.auth_cookie_name):
            await self.app(scope, receive, send)
            return

        if "sec-fetch-site" in headers:
            sec_fetch_site = (headers.get("sec-fetch-site") or "").strip().lower()
            if sec_fetch_site not in ALLOWED_SEC_FETCH_SITES:
                response = JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Cross-site request blocked"},
                )
                await response(scope, receive, send)
                return
        elif "origin" in headers:
            origin = (headers.get("origin") or "").strip().rstrip("/").lower()
            allowed_origins = _allowed_csrf_origins(scope, headers, cfg)
            if not origin or origin == "null" or origin not in allowed_origins:
                response = JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Cross-site request blocked"},
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


class PathAwareCORSMiddleware:
    """Path-aware ASGI CORS middleware preserving strict cookie CORS on ``/api/*``.

    - On Bearer-only / public discovery paths (``/mcp``, ``/mcp/*``, ``/.well-known/oauth-*``):
      bypasses credentialed Starlette ``CORSMiddleware`` so cross-origin MCP clients receive
      clean non-credentialed CORS headers (reflected sanitized bare origin or ``*``) and
      ``Access-Control-Expose-Headers`` across ``200``, ``204``, ``401``, ``403``, ``413``,
      and ``429`` responses without duplicate ``Access-Control-Allow-Origin`` headers.
    - On all other paths (including ``/api/*``): delegates unchanged to Starlette's
      ``CORSMiddleware`` with ``allow_credentials=True`` and configured ``allow_origins``.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        allow_origins: list[str],
    ) -> None:
        self.app = app
        self._api_cors = CORSMiddleware(
            app,
            allow_origins=allow_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", "/"))
        mcp_route = is_mcp_path(path)
        discovery_route = is_oauth_well_known_path(path)
        if not (mcp_route or discovery_route):
            await self._api_cors(scope, receive, send)
            return

        req_headers = Headers(scope=scope)
        raw_origin = (req_headers.get("origin") or "").strip()
        bare_origin = extract_bare_origin(raw_origin) if raw_origin else None
        allow_origin = (
            bare_origin
            if (bare_origin is not None and bare_origin == raw_origin.lower())
            else "*"
        )
        allow_methods = "GET, POST, DELETE, OPTIONS" if mcp_route else "GET, OPTIONS"

        async def send_with_mcp_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                resp_headers = MutableHeaders(scope=message)
                # Never advertise cookie credentials on Bearer-only / discovery routes
                if "access-control-allow-credentials" in resp_headers:
                    del resp_headers["access-control-allow-credentials"]
                if "access-control-allow-origin" not in resp_headers:
                    resp_headers["Access-Control-Allow-Origin"] = allow_origin
                if "access-control-allow-methods" not in resp_headers:
                    resp_headers["Access-Control-Allow-Methods"] = allow_methods
                if "access-control-allow-headers" not in resp_headers:
                    resp_headers["Access-Control-Allow-Headers"] = MCP_CORS_ALLOWED_HEADERS
                if "access-control-expose-headers" not in resp_headers:
                    resp_headers["Access-Control-Expose-Headers"] = MCP_CORS_EXPOSED_HEADERS
                if "vary" not in resp_headers:
                    resp_headers["Vary"] = "Origin"
            await send(message)

        await self.app(scope, receive, send_with_mcp_cors)


class SecurityHeadersMiddleware:
    """Pure ASGI middleware emitting baseline security response headers on all responses."""

    def __init__(self, app: ASGIApp, *, settings: Settings | None = None) -> None:
        self.app = app
        self._explicit_settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cfg = _resolve_settings_from_scope(scope, self._explicit_settings)
        req_headers = Headers(scope=scope)
        xf_proto = (req_headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
        is_https = (
            cfg.auth_cookie_secure
            or str(scope.get("scheme", "")).lower() == "https"
            or xf_proto == "https"
        )

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                resp_headers = MutableHeaders(scope=message)
                for hdr_key, hdr_val in SECURITY_RESPONSE_HEADERS:
                    if hdr_key.lower() not in resp_headers:
                        resp_headers[hdr_key] = hdr_val
                if is_https and "strict-transport-security" not in resp_headers:
                    resp_headers["Strict-Transport-Security"] = HSTS_HEADER_VALUE
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


class UsageAndRateLimitMiddleware:
    """Pure ASGI middleware enforcing origin secret, per-IP sliding-window limits & telemetry."""

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
        return _resolve_settings_from_scope(scope, self._explicit_settings)

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
        normalized_path = path.rstrip("/") or "/"
        is_api_route = normalized_path == "/api" or normalized_path.startswith("/api/")
        is_mcp_route = is_mcp_path(normalized_path)
        is_well_known_oauth = is_oauth_well_known_path(normalized_path)
        is_tracked_route = is_api_route or is_mcp_route or is_well_known_oauth
        is_options = method == "OPTIONS"
        route_group = classify_route_group(normalized_path)

        cfg = self._resolve_settings(scope)
        headers = Headers(scope=scope)

        # Shared origin secret check blocks direct unauthenticated origin access (e.g. Railway URL)
        expected_origin_secret = cfg.origin_shared_secret.strip()
        if expected_origin_secret and normalized_path not in ORIGIN_SECRET_BYPASS_PATHS:
            provided_secret = headers.get(ORIGIN_SECRET_HEADER, "")
            if not provided_secret or not hmac.compare_digest(
                provided_secret.encode("utf-8"),
                expected_origin_secret.encode("utf-8"),
            ):
                forbidden = JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Direct origin access forbidden"},
                )
                if is_tracked_route and not is_options:
                    self.usage_store.record(
                        route_group=route_group,
                        status_code=status.HTTP_403_FORBIDDEN,
                        bytes_out=len(forbidden.body),
                        rate_limited=False,
                    )
                    self.usage_store.maybe_flush()
                await forbidden(scope, receive, send)
                return

        is_bypass = (
            is_options
            or (not is_tracked_route)
            or (normalized_path in RATE_LIMIT_BYPASS_PATHS)
        )
        rl_headers: dict[str, str] = {}

        if cfg.rate_limit_enabled and not is_bypass:
            client_tuple = scope.get("client")
            client_host = client_tuple[0] if client_tuple else None
            has_proxy_ip_header = (
                ("cf-connecting-ip" in headers) or ("x-forwarded-for" in headers)
            )
            client_ip = extract_client_ip_from_headers(
                headers,
                client_host=client_host,
                trust_proxy_headers=cfg.trust_proxy_headers,
            )

            window_s = _MINUTE_WINDOW_S
            if is_mcp_route:
                bucket_name = "mcp"
                limit = cfg.rate_limit_mcp_per_min
            elif method == "POST" and normalized_path == OAUTH_DCR_PATH:
                bucket_name = "oauth_dcr"
                limit = cfg.oauth_dcr_max_per_ip_per_day
                window_s = _DAY_WINDOW_S
            elif method == "POST" and normalized_path in AUTH_RATE_LIMIT_PATHS:
                bucket_name = "auth"
                limit = cfg.rate_limit_auth_per_min
            else:
                bucket_name = "api"
                limit = cfg.rate_limit_read_per_min

            # Never bypass rate limiting based on header-derived client_ip == "testclient".
            # Only exempt Starlette TestClient when the raw ASGI peer is "testclient" AND
            # no proxy IP headers were supplied (and tests did not opt into enforcement).
            bypass_testclient = (
                client_host == "testclient"
                and not has_proxy_ip_header
                and not self._should_enforce_for_testclient(scope)
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
                    window_s=window_s,
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
            if is_tracked_route and not is_options:
                self.usage_store.record(
                    route_group=route_group,
                    status_code=status_code,
                    bytes_out=bytes_out,
                    rate_limited=(status_code == status.HTTP_429_TOO_MANY_REQUESTS),
                )
                self.usage_store.maybe_flush()
