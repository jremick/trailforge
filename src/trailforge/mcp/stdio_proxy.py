"""Lightweight local `stdio` MCP bridge (`trailforge-mcp`) + `--direct` PostGIS mode.

Usage:
    trailforge-mcp  # Proxy to $TRAILFORGE_MCP_URL (default http://localhost:5173/mcp)
    trailforge-mcp --url https://trails.example.com/mcp --token tf_pat_...
    trailforge-mcp --direct  # Run TrailForge MCP server directly against local DATABASE_URL
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from typing import Any, Final

import anyio
import httpx
from mcp import types
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

from trailforge.mcp.server import create_mcp_server

logger = logging.getLogger(__name__)

DEFAULT_MCP_URL: Final[str] = "http://localhost:5173/mcp"
DEFAULT_TIMEOUT_S: Final[float] = 60.0


def _parse_sse_jsonrpc_messages(text: str) -> list[types.JSONRPCMessage]:
    """Extract JSON-RPC messages from an HTTP `text/event-stream` response body."""
    messages: list[types.JSONRPCMessage] = []
    data_lines: list[str] = []

    def _flush() -> None:
        if not data_lines:
            return
        raw = "\n".join(data_lines).strip()
        data_lines.clear()
        if not raw:
            return
        parsed = json.loads(raw)
        messages.append(types.JSONRPCMessage.model_validate(parsed))

    for line in text.splitlines():
        if not line.strip():
            _flush()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    _flush()
    return messages


def _make_jsonrpc_error_reply(
    request_id: str | int,
    code: int,
    message: str,
) -> types.JSONRPCMessage:
    return types.JSONRPCMessage(
        types.JSONRPCError(
            jsonrpc="2.0",
            id=request_id,
            error=types.ErrorData(code=code, message=message),
        )
    )


async def forward_jsonrpc_message(
    client: httpx.AsyncClient,
    url: str,
    message: types.JSONRPCMessage,
    *,
    api_token: str | None = None,
    protocol_version: str | None = None,
) -> list[types.JSONRPCMessage]:
    """Forward one MCP JSON-RPC message over Streamable HTTP and return reply message(s)."""
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    if protocol_version:
        headers["MCP-Protocol-Version"] = protocol_version

    payload = message.model_dump(by_alias=True, exclude_none=True)
    req_id = payload.get("id")

    try:
        resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        if req_id is not None:
            return [
                _make_jsonrpc_error_reply(req_id, -32000, f"Upstream MCP connection failed: {exc}")
            ]
        return []

    # Notifications commonly return 202 Accepted with an empty body
    if resp.status_code in (202, 204) or not resp.content:
        return []

    if resp.status_code >= 400:
        detail = resp.text.strip()[:300] or f"HTTP {resp.status_code}"
        if req_id is not None:
            return [
                _make_jsonrpc_error_reply(
                    req_id,
                    -32001 if resp.status_code == 401 else -32000,
                    f"Upstream MCP returned HTTP {resp.status_code}: {detail}",
                )
            ]
        return []

    content_type = (resp.headers.get("content-type") or "").lower()
    if "text/event-stream" in content_type:
        return _parse_sse_jsonrpc_messages(resp.text)

    data = resp.json()
    if isinstance(data, list):
        return [types.JSONRPCMessage.model_validate(item) for item in data]
    return [types.JSONRPCMessage.model_validate(data)]


async def run_stdio_proxy(
    *,
    url: str = DEFAULT_MCP_URL,
    api_token: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Bridge local stdin/stdout MCP framing to a remote Streamable HTTP TrailForge endpoint."""
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_s, follow_redirects=False)
    protocol_version: str | None = None

    try:
        async with stdio_server() as (read_stream, write_stream):
            async for item in read_stream:
                if isinstance(item, Exception):
                    logger.warning("Ignoring malformed stdin MCP message: %s", item)
                    continue
                msg = item.message
                root: Any = msg.root
                if isinstance(root, types.JSONRPCRequest) and root.method == "initialize":
                    params = root.params or {}
                    pv = params.get("protocolVersion")
                    if isinstance(pv, str) and pv.strip():
                        protocol_version = pv.strip()

                replies = await forward_jsonrpc_message(
                    client,
                    url,
                    msg,
                    api_token=api_token,
                    protocol_version=protocol_version,
                )
                for reply in replies:
                    await write_stream.send(SessionMessage(reply))
    finally:
        if owns_client:
            await client.aclose()


async def run_direct_stdio() -> None:
    """Run TrailForge's MCP Server directly in-process over stdio against local DATABASE_URL."""
    server = create_mcp_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
            stateless=True,
        )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trailforge-mcp",
        description=(
            "TrailForge local stdio MCP bridge (proxies to Streamable HTTP or runs --direct)."
        ),
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("TRAILFORGE_MCP_URL") or DEFAULT_MCP_URL,
        help=f"Remote or local TrailForge Streamable HTTP MCP URL (default: {DEFAULT_MCP_URL}).",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("TRAILFORGE_API_TOKEN") or None,
        help="Optional TrailForge Bearer token (PAT tf_pat_... or OAuth access token).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help="HTTP request timeout in seconds (default: 60).",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Run the TrailForge MCP server directly in-process over stdio using DATABASE_URL.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entrypoint (`trailforge-mcp`)."""
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    if args.direct:
        anyio.run(run_direct_stdio)
    else:
        anyio.run(
            lambda: run_stdio_proxy(
                url=args.url,
                api_token=args.token,
                timeout_s=args.timeout,
            )
        )


if __name__ == "__main__":
    main()
