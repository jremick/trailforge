# TrailForge Antigravity Plugin Template

This directory is a ready-to-use **Google Antigravity (AGY)** plugin template that connects Antigravity to your **TrailForge** MCP server (`Streamable HTTP` or local `stdio` bridge), enabling:

- Inline interactive **SEP-1865 MCP App** maps and synchronized elevation profiles (`ui://trailforge/explorer.html`)
- A live-updating **TrailForge Map** side panel (`aux_pane` entrypoint via `open_trailforge_map`)
- Natural-language 4WD track search, explainable DEM/telemetry grading, live Australian agency closures, rig suitability checks, multi-track route planning, GPX/GeoJSON export, and GridForge 300 DPI handoff

## Quick Install (Global Plugin)

Copy this directory into `~/.gemini/config/plugins/trailforge/`:

```bash
mkdir -p ~/.gemini/config/plugins
cp -R deploy/ai/antigravity-plugin ~/.gemini/config/plugins/trailforge
```

Then edit `~/.gemini/config/plugins/trailforge/mcp_config.json` and replace `https://trails.example.com/mcp` with your TrailForge instance URL (or `http://localhost:5173/mcp` for a local Docker Compose instance).

Antigravity automatically discovers plugins placed under `~/.gemini/config/plugins/` without requiring a restart. If you edit `mcp_config.json` after the server is already running, refresh the MCP server from **MCP Servers** settings in Antigravity.

## Connection Modes (`mcp_config.json`)

### 1. Remote Streamable HTTP — Anonymous Public Intelligence (Default)

Works out of the box with zero local Python packages installed:

```json
{
  "mcpServers": {
    "trailforge": {
      "url": "https://trails.example.com/mcp"
    }
  }
}
```

### 2. Remote Streamable HTTP — OAuth 2.1 Sign-In (`/mcp/auth`)

To unlock personal garage rigs (`get_my_garage`), saved routes (`list_my_saved_routes`, `save_planned_route`), personal pins, favorites, and private trip summaries via self-hosted OAuth 2.1 + PKCE, point `url` to `/mcp/auth`:

```json
{
  "mcpServers": {
    "trailforge": {
      "url": "https://trails.example.com/mcp/auth"
    }
  }
}
```

### 3. Local `stdio` Bridge (`trailforge-mcp`) with Personal Access Token (PAT)

If you prefer authenticating with a scoped Personal Access Token (`tf_pat_...` minted in TrailForge's **Account Panel -> AI & MCP Access Tokens**) or running against a local PostGIS instance, use the `trailforge-mcp` `stdio` bridge.

Never hardcode workstation paths or real tokens into tracked configs. Export `TRAILFORGE_API_TOKEN` in your shell environment (or pass it through a local ignored script under `${PLUGIN_ROOT}/bin/`):

```json
{
  "mcpServers": {
    "trailforge": {
      "command": "trailforge-mcp",
      "args": [],
      "env": {
        "TRAILFORGE_MCP_URL": "https://trails.example.com/mcp",
        "TRAILFORGE_API_TOKEN": "${TRAILFORGE_API_TOKEN}"
      }
    }
  }
}
```

If you wrap a virtualenv launcher script inside the plugin folder, use Antigravity's `${PLUGIN_ROOT}` and `${PLUGIN_DATA}` expansion tokens instead of absolute paths:

```json
{
  "mcpServers": {
    "trailforge": {
      "command": "bash",
      "args": ["${PLUGIN_ROOT}/bin/run-trailforge-mcp.sh"],
      "env": {
        "TRAILFORGE_MCP_URL": "https://trails.example.com/mcp",
        "TRAILFORGE_API_TOKEN": "${TRAILFORGE_API_TOKEN}",
        "TRAILFORGE_PLUGIN_DATA": "${PLUGIN_DATA}"
      }
    }
  }
}
```

For local PostGIS development without an HTTP server, pass `--direct` to `trailforge-mcp` with `DATABASE_URL` set in your environment.

## Side Panel (`TrailForge Map` `aux_pane`)

When connected, `open_trailforge_map` exposes Antigravity's `com.google.antigravity/ui` `aux_pane` entrypoint (`title: "TrailForge Map"`). You can open it from the side pane **+** menu or let the agent drive it live as you search tracks, inspect grades, and plan routes.

## Full Documentation & Attributions

See [`docs/AI_INTEGRATION.md`](../../../docs/AI_INTEGRATION.md) for setup guides across ChatGPT, Claude, Gemini, Cursor, VS Code, Cloudflare Pages, and R2 CORS trade-offs.

TrailForge is licensed under **`GPL-3.0-or-later`**. Map and trail outputs preserve required upstream attributions for OpenStreetMap (`ODbL`), Geoscience Australia / DEA / CAPAD / Australian state and territory agencies (`CC BY 4.0`, `CC BY 3.0 AU` for Tasmania), and Open-Meteo (`CC BY 4.0`).
