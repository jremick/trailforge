"""Offline tests for MCP/OAuth edge proxies, R2 CORS, Antigravity plugin, and AI docs."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
IN_REPO_CHECKOUT = (REPO_ROOT / "docker-compose.yml").is_file()

requires_repo_checkout = pytest.mark.skipif(
    not IN_REPO_CHECKOUT,
    reason="Repo-root deployment manifests are not copied into the minimal runtime container image",
)

MCP_ENV_VARS_DEFAULTS: dict[str, str] = {
    "TRAILFORGE_ENABLE_MCP": "true",
    "TRAILFORGE_MCP_AUTH_MODE": "hybrid",
    "TRAILFORGE_RATE_LIMIT_MCP_PER_MIN": "60",
    "TRAILFORGE_MCP_APP_HTML": "",
    "TRAILFORGE_MCP_CSP_TILE_DOMAINS": "",
    "TRAILFORGE_OAUTH_ACCESS_TOKEN_TTL_S": "3600",
    "TRAILFORGE_OAUTH_REFRESH_TOKEN_TTL_S": "2592000",
    "TRAILFORGE_OAUTH_DCR_MAX_PER_IP_PER_DAY": "20",
}

W4_OWNED_FILES = (
    "deploy/cloudflare/pages-proxy-worker.js",
    "deploy/cloudflare/pages-middleware.js",
    "deploy/cloudflare/r2-cors-mcp.json",
    "web/nginx.conf",
    "web/vite.config.ts",
    "deploy/Caddyfile.example",
    ".env.example",
    "deploy/railway/env.railway.example",
    "deploy/ai/antigravity-plugin/plugin.json",
    "deploy/ai/antigravity-plugin/mcp_config.json",
    "deploy/ai/antigravity-plugin/assets/logo.svg",
    "deploy/ai/antigravity-plugin/README.md",
    "docs/AI_INTEGRATION.md",
)

ALLOWED_PLUGIN_TOP_LEVEL_KEYS = {
    "name",
    "displayName",
    "description",
    "version",
    "logo",
    "suggestedPrompts",
    "author",
    "homepage",
    "repository",
    "disabled",
}


def _parse_dotenv(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        parsed[key.strip()] = val.strip()
    return parsed


def _extract_nginx_location_block(nginx_conf: str, header: str) -> str:
    idx = nginx_conf.find(header)
    assert idx != -1, f"Missing nginx block {header!r}"
    brace_start = nginx_conf.find("{", idx)
    assert brace_start != -1, f"Malformed nginx block {header!r}"
    depth = 0
    for pos in range(brace_start, len(nginx_conf)):
        ch = nginx_conf[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return nginx_conf[brace_start : pos + 1]
    raise AssertionError(f"Unclosed nginx block {header!r}")


@requires_repo_checkout
def test_cloudflare_pages_proxy_worker_and_middleware_mcp_runtime() -> None:
    worker_path = REPO_ROOT / "deploy" / "cloudflare" / "pages-proxy-worker.js"
    middleware_path = REPO_ROOT / "deploy" / "cloudflare" / "pages-middleware.js"
    assert worker_path.is_file()
    assert middleware_path.is_file()

    worker_src = worker_path.read_text(encoding="utf-8")
    middleware_src = middleware_path.read_text(encoding="utf-8")

    # Static streaming, header, and error-handling invariants
    assert "arrayBuffer(" not in worker_src
    assert "upstreamResponse.body" in worker_src
    assert "export function isProxiedPath(" in worker_src
    assert "export async function onRequest(context)" in worker_src
    assert "export async function onRequest(context)" in middleware_src
    assert "./pages-proxy-worker.js" in middleware_src

    worker_lower = worker_src.lower()
    for required_hdr in (
        "mcp-session-id",
        "mcp-protocol-version",
        "accept",
        "last-event-id",
        "www-authenticate",
        "authorization",
        "cf-connecting-ip",
    ):
        assert required_hdr in worker_lower, f"Worker missing {required_hdr!r}"

    # No empty JS catch blocks in either worker or middleware
    empty_catch_re = re.compile(r"catch\s*(?:\([^)]*\))?\s*\{\s*\}")
    assert empty_catch_re.search(worker_src) is None
    assert empty_catch_re.search(middleware_src) is None

    node_bin = shutil.which("node")
    assert node_bin is not None, "Node.js is required to verify Cloudflare worker ESM behavior"

    for js_file in (worker_path, middleware_path):
        chk = subprocess.run(
            [node_bin, "--check", str(js_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert chk.returncode == 0, f"node --check failed for {js_file}:\n{chk.stderr}"

    worker_uri = worker_path.resolve().as_uri()
    middleware_uri = middleware_path.resolve().as_uri()

    node_script = f"""
import assert from "node:assert/strict";
import {{ isProxiedPath, onRequest as workerOnRequest }} from {json.dumps(worker_uri)};
import {{ onRequest as middlewareOnRequest }} from {json.dumps(middleware_uri)};

// 1. Path allowlist checks
for (const allowed of [
  "/api",
  "/api/health",
  "/api/oauth/token",
  "/mcp",
  "/mcp/auth",
  "/mcp/extra",
  "/.well-known/oauth-protected-resource",
  "/.well-known/oauth-protected-resource/mcp",
  "/.well-known/oauth-protected-resource/mcp/auth",
  "/.well-known/oauth-authorization-server",
]) {{
  assert.equal(isProxiedPath(allowed), true, `Expected proxied path: ${{allowed}}`);
}}
for (const denied of [
  "/",
  "/index.html",
  "/assets/app.js",
  "/api-fake",
  "/mcp-fake",
  "/.well-known/security.txt",
  "/pages-proxy-worker",
]) {{
  assert.equal(isProxiedPath(denied), false, `Expected static/non-proxied path: ${{denied}}`);
}}

const captured = [];
globalThis.fetch = async (url, init) => {{
  const hdrs = new Headers(init.headers);
  captured.push({{ url, method: init.method, headers: hdrs }});
  const parsed = new URL(url);

  if (parsed.pathname === "/mcp" && init.method === "POST") {{
    const stream = new ReadableStream({{
      start(controller) {{
        const enc = new TextEncoder();
        const payload = 'event: message\\ndata: {{"jsonrpc":"2.0","id":1}}\\n\\n';
        controller.enqueue(enc.encode(payload));
        controller.close();
      }},
    }});
    return new Response(stream, {{
      status: 200,
      headers: {{
        "Content-Type": "text/event-stream",
        "Mcp-Session-Id": "sess-999",
        "Access-Control-Expose-Headers": "Mcp-Session-Id, WWW-Authenticate",
      }},
    }});
  }}

  if (parsed.pathname === "/mcp/auth") {{
    const metaUrl = "https://trails.example.com/.well-known/oauth-protected-resource/mcp/auth";
    return new Response(JSON.stringify({{ detail: "Unauthorized" }}), {{
      status: 401,
      headers: {{
        "Content-Type": "application/json",
        "WWW-Authenticate":
          `Bearer resource_metadata="${{metaUrl}}", scope="trailforge:read trailforge:write"`,
      }},
    }});
  }}

  if (parsed.pathname === "/mcp/") {{
    return new Response(null, {{
      status: 307,
      headers: {{
        Location: "http://internal-svc.up.railway.app/mcp",
      }},
    }});
  }}

  if (parsed.pathname.startsWith("/.well-known/oauth-")) {{
    return new Response(
      JSON.stringify({{ issuer: "https://trails.example.com", resource: parsed.pathname }}),
      {{
        status: 200,
        headers: {{
          "Content-Type": "application/json",
          "Access-Control-Allow-Origin": "*",
        }},
      }},
    );
  }}

  if (parsed.pathname === "/mcp/upstream-error") {{
    return new Response("Internal Railway stack trace internal-svc.up.railway.app", {{
      status: 502,
      headers: {{ "Content-Type": "text/plain" }},
    }});
  }}

  return new Response(JSON.stringify({{ ok: true, path: parsed.pathname }}), {{
    status: 200,
    headers: {{ "Content-Type": "application/json" }},
  }});
}};

const env = {{
  RAILWAY_API_ORIGIN: "https://internal-svc.up.railway.app",
  ORIGIN_SHARED_SECRET: "edge-shared-secret-0123456789abcdef",
}};

// 2. POST /mcp streams SSE unbuffered, forwards MCP headers & strips spoofed origin secret
const mcpReq = new Request("https://trails.example.com/mcp", {{
  method: "POST",
  headers: {{
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
    Authorization: "Bearer example-pat-placeholder",
    "Mcp-Session-Id": "client-sess-42",
    "MCP-Protocol-Version": "2025-03-26",
    "Last-Event-ID": "evt-7",
    "CF-Connecting-IP": "198.51.100.77",
    "X-TrailForge-Origin-Secret": "spoofed-secret",
  }},
  body: JSON.stringify({{ jsonrpc: "2.0", id: 1, method: "initialize" }}),
}});
const mcpRes = await middlewareOnRequest({{
  request: mcpReq,
  env,
  next: () => {{
    throw new Error("should not call next");
  }},
}});
assert.equal(mcpRes.status, 200);
assert.equal(mcpRes.headers.get("mcp-session-id"), "sess-999");
assert.equal(mcpRes.headers.get("cache-control"), "no-cache, no-transform");
assert.equal(mcpRes.headers.get("x-content-type-options"), "nosniff");
const sseText = await mcpRes.text();
assert.ok(sseText.includes('"jsonrpc":"2.0"'));

const fwdHeaders = captured[0].headers;
assert.equal(fwdHeaders.get("mcp-session-id"), "client-sess-42");
assert.equal(fwdHeaders.get("mcp-protocol-version"), "2025-03-26");
assert.equal(fwdHeaders.get("last-event-id"), "evt-7");
assert.equal(fwdHeaders.get("accept"), "application/json, text/event-stream");
assert.equal(fwdHeaders.get("authorization"), "Bearer example-pat-placeholder");
assert.equal(fwdHeaders.get("x-trailforge-origin-secret"), env.ORIGIN_SHARED_SECRET);
assert.equal(fwdHeaders.get("x-forwarded-host"), "trails.example.com");
assert.equal(fwdHeaders.get("x-forwarded-proto"), "https");

// 3. /mcp/auth preserves 401 and WWW-Authenticate RFC 9728 challenge
const authRes = await middlewareOnRequest({{
  request: new Request("https://trails.example.com/mcp/auth", {{ method: "POST" }}),
  env,
}});
assert.equal(authRes.status, 401);
const wwwAuth = authRes.headers.get("www-authenticate");
assert.ok(wwwAuth && wwwAuth.includes("/.well-known/oauth-protected-resource/mcp/auth"));

// 4. /.well-known/oauth-* discovery paths proxy cleanly
for (const wkPath of [
  "/.well-known/oauth-protected-resource",
  "/.well-known/oauth-protected-resource/mcp",
  "/.well-known/oauth-protected-resource/mcp/auth",
  "/.well-known/oauth-authorization-server",
]) {{
  const wkRes = await middlewareOnRequest({{
    request: new Request(`https://trails.example.com${{wkPath}}`),
    env,
  }});
  assert.equal(wkRes.status, 200);
  const wkJson = await wkRes.json();
  assert.equal(wkJson.resource, wkPath);
}}

// 5. Redirect rewriting works on /mcp/ too (never leaks *.up.railway.app)
const redirRes = await middlewareOnRequest({{
  request: new Request("https://trails.example.com/mcp/"),
  env,
}});
assert.equal(redirRes.status, 307);
assert.equal(redirRes.headers.get("location"), "https://trails.example.com/mcp");

// 6. Sanitized 502 error on /mcp/upstream-error never exposes internal origin
const errRes = await middlewareOnRequest({{
  request: new Request("https://trails.example.com/mcp/upstream-error"),
  env,
}});
assert.equal(errRes.status, 502);
const errText = await errRes.text();
assert.ok(!errText.includes("up.railway.app"));

// 7. Static SPA routes call context.next() and never invoke upstream fetch
const callsBeforeStatic = captured.length;
let nextCalled = 0;
const spaRes = await middlewareOnRequest({{
  request: new Request("https://trails.example.com/assets/main.js"),
  env,
  next: async () => {{
    nextCalled += 1;
    return new Response("console.log('spa');", {{ status: 200 }});
  }},
}});
assert.equal(nextCalled, 1);
assert.equal(spaRes.status, 200);
assert.equal(captured.length, callsBeforeStatic);

// 8. Standalone worker returns 404 for non-proxied path when context.next is absent
const notFoundRes = await workerOnRequest({{
  request: new Request("https://trails.example.com/not-proxied"),
  env,
}});
assert.equal(notFoundRes.status, 404);
"""

    run = subprocess.run(
        [node_bin, "--input-type=module", "-e", node_script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, f"Node proxy runtime checks failed:\n{run.stdout}\n{run.stderr}"


@requires_repo_checkout
def test_r2_cors_mcp_opt_in_wildcard_readonly_and_default_r2_cors_untouched() -> None:
    default_path = REPO_ROOT / "deploy" / "cloudflare" / "r2-cors.json"
    mcp_path = REPO_ROOT / "deploy" / "cloudflare" / "r2-cors-mcp.json"
    assert default_path.is_file()
    assert mcp_path.is_file()

    # Default R2 CORS remains strict single-origin
    default_rules = json.loads(default_path.read_text(encoding="utf-8"))
    assert default_rules[0]["AllowedOrigins"] == ["https://trails.example.com"]
    assert "*" not in default_rules[0]["AllowedOrigins"]

    # Opt-in MCP R2 CORS uses wildcard origin strictly for read-only GET/HEAD Range requests
    mcp_rules = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert isinstance(mcp_rules, list) and len(mcp_rules) == 1
    rule = mcp_rules[0]
    assert rule["AllowedOrigins"] == ["*"]
    methods = set(rule["AllowedMethods"])
    assert methods == {"GET", "HEAD"}

    allowed_headers = {h.lower() for h in rule["AllowedHeaders"]}
    for required in ("range", "if-range", "if-none-match", "accept", "origin"):
        assert required in allowed_headers

    expose_headers = {h.lower() for h in rule["ExposeHeaders"]}
    for required in ("content-range", "content-length", "accept-ranges", "etag"):
        assert required in expose_headers

    assert isinstance(rule["MaxAgeSeconds"], int) and rule["MaxAgeSeconds"] > 0


@requires_repo_checkout
def test_nginx_conf_mcp_and_oauth_unbuffered_proxy_blocks() -> None:
    nginx_path = REPO_ROOT / "web" / "nginx.conf"
    conf = nginx_path.read_text(encoding="utf-8")

    # Sanity-check balanced braces
    assert conf.count("{") == conf.count("}")

    mcp_block = _extract_nginx_location_block(conf, "location ~ ^/mcp(/|$)")
    oauth_block = _extract_nginx_location_block(conf, r"location ~ ^/\.well-known/oauth-")

    for block_name, block in (("mcp", mcp_block), ("oauth", oauth_block)):
        assert "http://api:8000" in block, f"{block_name} missing api:8000 upstream"
        assert "proxy_http_version 1.1;" in block
        assert "proxy_buffering off;" in block
        assert "proxy_request_buffering off;" in block
        assert "proxy_pass_header WWW-Authenticate;" in block
        assert "proxy_set_header X-Forwarded-Host $host;" in block
        assert "proxy_set_header Authorization $http_authorization;" in block
        assert "proxy_set_header Accept $http_accept;" in block

    assert "gzip off;" in mcp_block
    assert "proxy_pass_header Mcp-Session-Id;" in mcp_block
    assert "proxy_set_header Mcp-Session-Id $http_mcp_session_id;" in mcp_block
    assert "proxy_set_header MCP-Protocol-Version $http_mcp_protocol_version;" in mcp_block
    assert "proxy_set_header Last-Event-ID $http_last_event_id;" in mcp_block

    # Existing blocks preserved intact
    for existing in (
        "location /api/tiles/",
        "location ~ ^/api/packs(/|$)",
        "location /api/",
        "location / {",
    ):
        assert existing in conf


@requires_repo_checkout
def test_vite_config_and_caddyfile_mcp_proxy_rules() -> None:
    vite_conf = (REPO_ROOT / "web" / "vite.config.ts").read_text(encoding="utf-8")
    for route in ('"/api"', '"/mcp"', '"/.well-known"'):
        assert route in vite_conf, f"web/vite.config.ts missing proxy rule for {route}"
    assert vite_conf.count("http://localhost:8000") >= 3

    caddy = (REPO_ROOT / "deploy" / "Caddyfile.example").read_text(encoding="utf-8")
    compressible_match = re.search(r"@compressible\s*\{([^}]+)\}", caddy)
    assert compressible_match is not None
    compressible_body = compressible_match.group(1)
    assert "/mcp" in compressible_body
    assert "/mcp/*" in compressible_body
    assert "/api/tiles/*" in compressible_body
    assert "flush_interval -1" in caddy
    assert "X-Forwarded-Host" in caddy
    for hdr in ("Mcp-Session-Id", "MCP-Protocol-Version", "Last-Event-ID", "WWW-Authenticate"):
        assert hdr in caddy, f"deploy/Caddyfile.example missing {hdr!r} documentation"


@requires_repo_checkout
def test_env_examples_document_all_nine_mcp_and_oauth_vars() -> None:
    local_env = _parse_dotenv(REPO_ROOT / ".env.example")
    railway_env = _parse_dotenv(REPO_ROOT / "deploy" / "railway" / "env.railway.example")

    for label, env_map in ((".env.example", local_env), ("env.railway.example", railway_env)):
        assert "TRAILFORGE_PUBLIC_BASE_URL" in env_map, (
            f"{label} missing TRAILFORGE_PUBLIC_BASE_URL"
        )
        for key, expected_default in MCP_ENV_VARS_DEFAULTS.items():
            assert key in env_map, f"{label} missing {key!r}"
            assert env_map[key] == expected_default, (
                f"{label} {key}={env_map[key]!r}, expected {expected_default!r}"
            )

    assert local_env["TRAILFORGE_PUBLIC_BASE_URL"] == ""
    assert railway_env["TRAILFORGE_PUBLIC_BASE_URL"] == "https://trails.example.com"


@requires_repo_checkout
def test_antigravity_plugin_template_manifest_mcp_config_logo_and_readme() -> None:
    plugin_dir = REPO_ROOT / "deploy" / "ai" / "antigravity-plugin"
    manifest_path = plugin_dir / "plugin.json"
    mcp_cfg_path = plugin_dir / "mcp_config.json"
    logo_path = plugin_dir / "assets" / "logo.svg"
    readme_path = plugin_dir / "README.md"

    for p in (manifest_path, mcp_cfg_path, logo_path, readme_path):
        assert p.is_file(), f"Missing Antigravity plugin file: {p.relative_to(REPO_ROOT)}"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest.keys()).issubset(ALLOWED_PLUGIN_TOP_LEVEL_KEYS)
    assert manifest["name"] == "trailforge"
    assert re.fullmatch(r"[a-z0-9-]+", manifest["name"])
    assert manifest["description"].strip()
    assert manifest["logo"] == "assets/logo.svg"
    assert not manifest["logo"].startswith(("/", "http://", "https://"))
    assert (plugin_dir / manifest["logo"]).is_file()

    prompts = manifest["suggestedPrompts"]
    assert isinstance(prompts, list) and 1 <= len(prompts) <= 3
    assert all(isinstance(item, str) and item.strip() for item in prompts)

    mcp_cfg = json.loads(mcp_cfg_path.read_text(encoding="utf-8"))
    assert "mcpServers" in mcp_cfg and isinstance(mcp_cfg["mcpServers"], dict)
    assert "trailforge" in mcp_cfg["mcpServers"]
    server_cfg = mcp_cfg["mcpServers"]["trailforge"]
    assert server_cfg["url"] == "https://trails.example.com/mcp"

    # SVG logo must parse cleanly as square >= 128x128 XML
    svg_root = ET.fromstring(logo_path.read_text(encoding="utf-8"))
    assert svg_root.tag.endswith("svg")
    width = int(svg_root.attrib["width"])
    height = int(svg_root.attrib["height"])
    assert width == height and width >= 128
    assert svg_root.attrib["viewBox"] == f"0 0 {width} {height}"

    readme = readme_path.read_text(encoding="utf-8")
    for token in (
        "~/.gemini/config/plugins/trailforge",
        "${PLUGIN_ROOT}",
        "${TRAILFORGE_API_TOKEN}",
        "trailforge-mcp",
        "aux_pane",
        "https://trails.example.com/mcp",
        "GPL-3.0-or-later",
        "ODbL",
        "CC BY 4.0",
    ):
        assert token in readme, f"Antigravity plugin README missing {token!r}"


@requires_repo_checkout
def test_ai_integration_docs_completeness_and_attributions() -> None:
    doc_path = REPO_ROOT / "docs" / "AI_INTEGRATION.md"
    assert doc_path.is_file(), "Missing docs/AI_INTEGRATION.md"
    doc = doc_path.read_text(encoding="utf-8")

    for required in (
        "ChatGPT",
        "Claude",
        "Claude Code",
        "Antigravity",
        "Gemini Enterprise",
        "Gemini CLI",
        "Cursor",
        "VS Code",
        "trailforge-mcp",
        "https://trails.example.com/mcp",
        "https://trails.example.com/mcp/auth",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "pages-middleware.js",
        "pages-proxy-worker.js",
        "r2-cors.json",
        "r2-cors-mcp.json",
        "TRAILFORGE_ENABLE_MCP",
        "TRAILFORGE_MCP_AUTH_MODE",
        "TRAILFORGE_PUBLIC_BASE_URL",
        "TRAILFORGE_RATE_LIMIT_MCP_PER_MIN",
        "TRAILFORGE_MCP_APP_HTML",
        "TRAILFORGE_MCP_CSP_TILE_DOMAINS",
        "TRAILFORGE_OAUTH_ACCESS_TOKEN_TTL_S",
        "TRAILFORGE_OAUTH_REFRESH_TOKEN_TTL_S",
        "TRAILFORGE_OAUTH_DCR_MAX_PER_IP_PER_DAY",
        "GPL-3.0-or-later",
        "OpenStreetMap",
        "ODbL",
        "Geoscience Australia",
        "CC BY 4.0",
        "CC BY 3.0 AU",
        "Open-Meteo",
    ):
        assert required in doc, f"docs/AI_INTEGRATION.md missing {required!r}"


@requires_repo_checkout
def test_w4_files_privacy_hygiene_zero_local_paths_or_secrets() -> None:
    forbidden_patterns = (
        re.compile(r"/[U]sers/"),
        re.compile(r"/[h]ome/[a-zA-Z0-9._-]+/"),
        re.compile(r"[C]:\\[U]sers\\"),
        re.compile(r"tf_pat_[A-Za-z0-9_-]{20,}"),
        re.compile(r"tf_oat_[A-Za-z0-9_-]{20,}"),
        re.compile(r"tf_ort_[A-Za-z0-9_-]{20,}"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"ghp_[0-9A-Za-z]{20,}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    )
    email_pattern = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    for rel in W4_OWNED_FILES:
        file_path = REPO_ROOT / rel
        assert file_path.is_file(), f"Missing W4 file: {rel}"
        text = file_path.read_text(encoding="utf-8")
        for pat in forbidden_patterns:
            assert pat.search(text) is None, f"Forbidden pattern {pat.pattern!r} found in {rel}"
        for email in email_pattern.findall(text):
            assert email.endswith("@example.com"), f"Non-placeholder email {email!r} found in {rel}"
