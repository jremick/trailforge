"""TrailForge Model Context Protocol (MCP) server, tools, resources, prompts & PNG visuals."""

from __future__ import annotations

from trailforge.mcp.context import (
    ANONYMOUS_CONTEXT,
    SCOPE_READ,
    SCOPE_WRITE,
    McpAuthContext,
    McpAuthError,
    McpAuthResolver,
    default_auth_resolver,
    get_mcp_auth_context,
)
from trailforge.mcp.server import (
    McpAsgiApps,
    create_mcp_asgi_apps,
    create_mcp_server,
    mcp_lifespan,
)

__all__ = [
    "ANONYMOUS_CONTEXT",
    "McpAsgiApps",
    "McpAuthContext",
    "McpAuthError",
    "McpAuthResolver",
    "SCOPE_READ",
    "SCOPE_WRITE",
    "create_mcp_asgi_apps",
    "create_mcp_server",
    "default_auth_resolver",
    "get_mcp_auth_context",
    "mcp_lifespan",
]
