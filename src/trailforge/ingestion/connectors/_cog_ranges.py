"""Serve a single configured COG to GDAL through bounded loopback range reads."""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from trailforge.ingestion.connectors.base import (
    IDENTITY_HTTP_HEADERS,
    checked_content_length,
    iter_bounded_response,
)

logger = logging.getLogger(__name__)

_MAX_RANGE_BYTES = 32 * 1024 * 1024
_MAX_READ_BYTES = 512 * 1024 * 1024
_MAX_READ_REQUESTS = 4096
_RANGE = re.compile(r"bytes=([0-9]{1,16})-([0-9]{1,16})")
_CONTENT_RANGE = re.compile(r"bytes ([0-9]{1,16})-([0-9]{1,16})/([0-9]{1,16})")


@dataclass
class CogRangeOperation:
    path: str
    failed: threading.Event = field(default_factory=threading.Event)


@contextmanager
def cog_range_path(
    url: str, client: httpx.Client, *, timeout_s: float
) -> Iterator[CogRangeOperation]:
    """Give GDAL a local file URL with a shared upstream request, byte and time budget."""
    upstream = httpx.URL(url)
    if upstream.scheme not in ("http", "https") or not upstream.host:
        raise ValueError("The configured COG source must be a direct HTTP(S) URL")
    path = f"/{secrets.token_urlsafe(24)}.tif"
    remaining_bytes = _MAX_READ_BYTES
    remaining_requests = _MAX_READ_REQUESTS
    deadline = time.monotonic() + timeout_s
    asset_size: int | None = None
    active_request = threading.Lock()
    stopped = threading.Event()

    class RangeHandler(BaseHTTPRequestHandler):
        timeout = 10.0

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_HEAD(self) -> None:
            self._serve_range()

        def do_GET(self) -> None:
            self._serve_range()

        def _send_range_headers(
            self, status: int, length: int, content_range: str | None = None
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if content_range is not None:
                self.send_header("Content-Range", content_range)
            self.end_headers()

        def _serve_range(self) -> None:
            if self.path != path:
                self.send_error(404)
                return
            # Serialize accounting, including metadata probes and repeated ranges.
            with active_request:
                self._forward()

        def _forward(self) -> None:
            nonlocal remaining_bytes, remaining_requests, asset_size
            headers_sent = False
            try:
                if stopped.is_set() or operation.failed.is_set() or time.monotonic() >= deadline:
                    raise ValueError("COG range transport is unavailable")
                is_head = self.command == "HEAD"
                if is_head and asset_size is not None:
                    headers_sent = True
                    self._send_range_headers(200, asset_size)
                    return
                if is_head:
                    # A one-byte probe also works for providers whose signed URLs only allow GET.
                    start, end = 0, 0
                else:
                    requested = _RANGE.fullmatch(self.headers.get("Range", ""))
                    if requested is None:
                        raise ValueError("COG reads require a single finite byte range")
                    start, end = map(int, requested.groups())
                if not 0 < end - start + 1 <= min(_MAX_RANGE_BYTES, remaining_bytes):
                    raise ValueError("COG range exceeds the byte budget")
                if remaining_requests <= 0:
                    raise ValueError("COG range exceeds the request budget")
                remaining_requests -= 1

                headers = {**IDENTITY_HTTP_HEADERS, "Range": f"bytes={start}-{end}"}
                with client.stream(
                    "GET", upstream, headers=headers, follow_redirects=False
                ) as resp:
                    actual = _CONTENT_RANGE.fullmatch(resp.headers.get("Content-Range", ""))
                    if resp.status_code != 206 or actual is None:
                        raise ValueError("COG provider did not return a direct byte range")
                    first, last, size = map(int, actual.groups())
                    length = last - first + 1
                    if (
                        first != start or last != min(end, size - 1) or length <= 0
                        or (asset_size is not None and size != asset_size)
                    ):
                        raise ValueError("COG provider range did not match the request")
                    encoding = resp.headers.get("Content-Encoding", "identity").strip().lower()
                    if encoding not in ("", "identity"):
                        raise ValueError("COG provider did not honor identity encoding")
                    declared = checked_content_length(resp, length)
                    if declared is not None and declared != length:
                        raise ValueError("COG provider sent an inconsistent range length")
                    time_left = deadline - time.monotonic()
                    if time_left <= 0 or stopped.is_set():
                        raise ValueError("COG range exceeded the time budget")
                    remaining_bytes -= length
                    if not is_head:
                        headers_sent = True
                        self._send_range_headers(206, length, f"bytes {first}-{last}/{size}")
                    received = 0
                    for chunk in iter_bounded_response(
                        resp, max_bytes=length, max_duration_s=time_left
                    ):
                        received += len(chunk)
                        if not is_head:
                            self.wfile.write(chunk)
                    if received != length:
                        raise ValueError("COG provider sent an incomplete range")
                    asset_size = size
                    if is_head:
                        headers_sent = True
                        self._send_range_headers(200, size)
            except (httpx.HTTPError, OSError, ValueError) as exc:
                operation.failed.set()
                logger.debug("COG range request failed (%s)", type(exc).__name__)
                if not headers_sent:
                    self.send_error(502)

    with ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler) as server:
        operation = CogRangeOperation(f"/vsicurl/http://127.0.0.1:{server.server_port}{path}")
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
        )
        thread.start()
        try:
            yield operation
        finally:
            stopped.set()
            server.shutdown()
            thread.join()
            with active_request:
                pass
