"""Offline validation tests for TrailForge Railway + Cloudflare cloud deploy kit."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
IN_REPO_CHECKOUT = (REPO_ROOT / "docker-compose.yml").is_file()

requires_repo_checkout = pytest.mark.skipif(
    not IN_REPO_CHECKOUT,
    reason="Repo-root deployment manifests are not copied into the minimal runtime container image",
)

HARDENING_ENV_VARS = (
    "TRAILFORGE_REGISTRATION_MODE",
    "TRAILFORGE_ADMIN_EMAIL",
    "TRAILFORGE_DEMO_BANNER",
    "TRAILFORGE_TRUST_PROXY_HEADERS",
    "TRAILFORGE_ORIGIN_SHARED_SECRET",
    "TRAILFORGE_ENABLE_DOCS",
    "TRAILFORGE_RATE_LIMIT_ENABLED",
    "TRAILFORGE_RATE_LIMIT_READ_PER_MIN",
    "TRAILFORGE_RATE_LIMIT_AUTH_PER_MIN",
    "TRAILFORGE_RATE_LIMIT_UPLOAD_PER_DAY",
    "TRAILFORGE_UPLOADS_MAX_TOTAL_MB",
    "TILES_PUBLIC_BASE_URL",
    "PACKS_PUBLIC_BASE_URL",
)

CLOUD_DEPLOY_FILES = (
    "railway.toml",
    "deploy/railway/README.md",
    "deploy/railway/env.railway.example",
    "deploy/cloudflare/r2-cors.json",
    "deploy/cloudflare/pages-proxy-worker.js",
    "deploy/cloudflare/waf-rules.md",
    ".env.example",
    "docker-compose.yml",
    "docker-compose.ghcr.yml",
)


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"Expected top-level YAML mapping in {path}"
    return data


def _parse_dotenv(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        parsed[key.strip()] = val.strip()
    return parsed


def _compose_api_env(path: Path) -> dict[str, str]:
    data = _load_yaml(path)
    env = data["services"]["api"]["environment"]
    assert isinstance(env, dict), f"Expected mapping environment on api in {path}"
    return {str(k): str(v) for k, v in env.items()}


@requires_repo_checkout
def test_railway_toml_valid_and_binds_dynamic_port() -> None:
    toml_path = REPO_ROOT / "railway.toml"
    assert toml_path.is_file(), "Missing railway.toml at repository root"

    cfg = tomllib.loads(toml_path.read_text(encoding="utf-8"))

    build = cfg["build"]
    assert build["builder"] == "DOCKERFILE"
    assert build["dockerfilePath"] == "Dockerfile"

    deploy = cfg["deploy"]
    start_cmd = deploy["startCommand"]
    assert "alembic upgrade head" in start_cmd
    assert "uvicorn trailforge.api.main:app" in start_cmd
    assert "--host 0.0.0.0" in start_cmd
    assert "${PORT:-8000}" in start_cmd

    assert deploy["healthcheckPath"] == "/api/health"
    assert deploy["healthcheckTimeout"] == 120
    assert deploy["restartPolicyType"] == "ON_FAILURE"
    assert deploy["restartPolicyMaxRetries"] == 5


@requires_repo_checkout
def test_cloudflare_r2_cors_policy_pmtiles_range_headers() -> None:
    cors_path = REPO_ROOT / "deploy" / "cloudflare" / "r2-cors.json"
    assert cors_path.is_file(), "Missing deploy/cloudflare/r2-cors.json"

    rules = json.loads(cors_path.read_text(encoding="utf-8"))
    assert isinstance(rules, list) and len(rules) >= 1

    rule = rules[0]
    origins = rule["AllowedOrigins"]
    assert isinstance(origins, list) and len(origins) >= 1
    assert all(origin.startswith("https://") for origin in origins)

    methods = set(rule["AllowedMethods"])
    assert {"GET", "HEAD"}.issubset(methods)
    assert not methods.intersection({"PUT", "POST", "DELETE", "PATCH"})

    allowed_headers = {h.lower() for h in rule["AllowedHeaders"]}
    for required in ("range", "if-range", "if-none-match"):
        assert required in allowed_headers, f"R2 CORS missing AllowedHeader {required!r}"

    expose_headers = {h.lower() for h in rule["ExposeHeaders"]}
    for required in ("content-range", "content-length", "accept-ranges", "etag"):
        assert required in expose_headers, f"R2 CORS missing ExposeHeader {required!r}"

    assert isinstance(rule["MaxAgeSeconds"], int) and rule["MaxAgeSeconds"] > 0


@requires_repo_checkout
def test_cloudflare_pages_proxy_worker_syntax_and_headers() -> None:
    worker_path = REPO_ROOT / "deploy" / "cloudflare" / "pages-proxy-worker.js"
    assert worker_path.is_file(), "Missing deploy/cloudflare/pages-proxy-worker.js"

    source = worker_path.read_text(encoding="utf-8")
    assert "export async function onRequest(context)" in source
    assert "RAILWAY_API_ORIGIN" in source
    assert "/api/" in source

    # Required forwarded headers & streaming/Set-Cookie invariants
    source_lower = source.lower()
    for header in ("cf-connecting-ip", "range", "if-range", "cookie", "authorization"):
        assert header in source_lower, f"Proxy worker missing header handling for {header!r}"
    assert "Set-Cookie" in source
    assert "upstreamResponse.body" in source
    assert "arrayBuffer(" not in source

    # Upstream error sanitization, origin secret injection & redirect rewriting
    assert "ORIGIN_SHARED_SECRET" in source
    assert "X-TrailForge-Origin-Secret" in source
    assert "rewriteRedirectLocation" in source
    assert "X-Content-Type-Options" in source
    assert "X-Frame-Options" in source
    assert "Referrer-Policy" in source
    assert "Strict-Transport-Security" in source
    assert "502" in source
    assert "503" in source
    assert "no-store" in source

    node_bin = shutil.which("node")
    if node_bin is not None:
        res = subprocess.run(
            [node_bin, "--check", str(worker_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, f"node --check failed:\n{res.stdout}\n{res.stderr}"

        # Runtime proof (M-2, H-1, M-3): 307 Location rewrites internal Railway host -> client
        # origin, injects X-TrailForge-Origin-Secret (stripping client spoofs), and emits
        # global security headers on proxied & error responses.
        worker_uri = worker_path.resolve().as_uri()
        node_script = f"""
import assert from "node:assert/strict";
import {{ onRequest }} from {json.dumps(worker_uri)};

const captured = [];
globalThis.fetch = async (url, init) => {{
  captured.push({{ url, headers: new Headers(init.headers) }});
  const parsed = new URL(url);
  if (parsed.pathname === "/api/packs/") {{
    return new Response(null, {{
      status: 307,
      headers: {{
        Location: "https://internal-secret-svc.up.railway.app/api/packs?region=alps",
      }},
    }});
  }}
  if (parsed.pathname === "/api/proto-rel") {{
    return new Response(null, {{
      status: 307,
      headers: {{
        Location: "//internal-secret-svc.up.railway.app/api/auth/login",
      }},
    }});
  }}
  if (parsed.pathname === "/api/external") {{
    return new Response(null, {{
      status: 302,
      headers: {{
        Location: "https://tiles.example.com/trailforge_vector.pmtiles",
      }},
    }});
  }}
  if (parsed.pathname === "/api/fail") {{
    throw new Error("ECONNREFUSED internal-secret-svc.up.railway.app");
  }}
  return new Response(JSON.stringify({{ ok: true }}), {{
    status: 200,
    headers: {{ "Content-Type": "application/json" }},
  }});
}};

const env = {{
  RAILWAY_API_ORIGIN: "https://internal-secret-svc.up.railway.app",
  ORIGIN_SHARED_SECRET: "shared-secret-value-0123456789",
}};

// 1. 307 Location rewritten from *.up.railway.app -> client origin + origin secret injected
const req1 = new Request("https://trails.example.com/api/packs/?region=alps", {{
  headers: {{
    "CF-Connecting-IP": "198.51.100.22",
    "X-TrailForge-Origin-Secret": "attacker-spoofed-secret",
  }},
}});
const res1 = await onRequest({{ request: req1, env }});
assert.equal(res1.status, 307);
assert.equal(res1.headers.get("location"), "https://trails.example.com/api/packs?region=alps");
assert.ok(!res1.headers.get("location").includes("up.railway.app"));
assert.equal(res1.headers.get("x-content-type-options"), "nosniff");
assert.equal(res1.headers.get("x-frame-options"), "DENY");
assert.equal(res1.headers.get("referrer-policy"), "strict-origin-when-cross-origin");
assert.equal(
  res1.headers.get("strict-transport-security"),
  "max-age=31536000; includeSubDomains",
);
assert.equal(
  captured[0].headers.get("x-trailforge-origin-secret"),
  "shared-secret-value-0123456789",
);

// 2. Protocol-relative upstream redirect also rewritten cleanly
const res2 = await onRequest({{
  request: new Request("https://trails.example.com/api/proto-rel"),
  env,
}});
assert.equal(res2.headers.get("location"), "https://trails.example.com/api/auth/login");

// 3. External R2 redirect left untouched
const res3 = await onRequest({{
  request: new Request("https://trails.example.com/api/external"),
  env,
}});
assert.equal(res3.headers.get("location"), "https://tiles.example.com/trailforge_vector.pmtiles");

// 4. Upstream 502 error sanitized & carries security headers
const res4 = await onRequest({{
  request: new Request("https://trails.example.com/api/fail"),
  env,
}});
assert.equal(res4.status, 502);
const errBody = await res4.text();
assert.ok(!errBody.includes("up.railway.app"));
assert.equal(res4.headers.get("x-content-type-options"), "nosniff");
assert.equal(res4.headers.get("x-frame-options"), "DENY");
"""
        runtime_res = subprocess.run(
            [node_bin, "--input-type=module", "-e", node_script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert runtime_res.returncode == 0, (
            f"Node worker runtime test failed:\n{runtime_res.stdout}\n{runtime_res.stderr}"
        )


@requires_repo_checkout
def test_railway_and_cloudflare_docs_completeness_and_privacy_hygiene() -> None:
    railway_readme = (REPO_ROOT / "deploy" / "railway" / "README.md").read_text(encoding="utf-8")
    railway_env_path = REPO_ROOT / "deploy" / "railway" / "env.railway.example"
    railway_env_text = railway_env_path.read_text(encoding="utf-8")
    waf_rules = (REPO_ROOT / "deploy" / "cloudflare" / "waf-rules.md").read_text(encoding="utf-8")

    # Railway production playbook completeness
    for token in (
        "imresamu/postgis:16-3.4",
        "/var/lib/postgresql/data",
        "ghcr.io/jremick/trailforge-backend:latest",
        "python -m trailforge.worker.main",
        "Hard Limit",
        "TRAILFORGE_REGISTRATION_MODE=invite_only",
        "TRAILFORGE_ADMIN_EMAIL",
        "AUTH_SECRET_KEY",
        "TRAILFORGE_PRIVACY_SECRET",
        "TRAILFORGE_ORIGIN_SHARED_SECRET",
        "ORIGIN_SHARED_SECRET",
        "TRAILFORGE_ENABLE_DOCS=false",
        "AUTH_COOKIE_SECURE=true",
        "TILES_PUBLIC_BASE_URL",
        "PACKS_PUBLIC_BASE_URL",
        "TRAILFORGE_DEMO_BANNER",
        "trailforge.packs.r2_sync",
        "trailforge.auth.cli create-invite",
        "GPL-3.0-or-later",
    ):
        assert token in railway_readme, f"deploy/railway/README.md missing {token!r}"

    # Railway env template checks
    railway_env = _parse_dotenv(railway_env_path)
    assert railway_env["TRAILFORGE_REGISTRATION_MODE"] == "invite_only"
    assert railway_env["AUTH_COOKIE_SECURE"] == "true"
    assert railway_env["DATABASE_URL"].startswith("postgresql+psycopg://")
    assert railway_env["TILES_PUBLIC_BASE_URL"].startswith("https://")
    assert railway_env["PACKS_PUBLIC_BASE_URL"].startswith("https://")
    for var in HARDENING_ENV_VARS:
        assert var in railway_env, f"env.railway.example missing {var!r}"
    assert "TRAILFORGE_R2_ENDPOINT_URL" in railway_env_text

    # Cloudflare WAF + Cache + Analytics recipe checks
    for token in (
        "/api/auth/",
        "/api/trips/",
        "/api/reports",
        "/api/",
        "10 req/min",
        "20 req/min",
        "120 req/min",
        "Bot Fight Mode",
        "Managed Challenge",
        "tiles.example.com",
        ".pmtiles",
        "Range",
        "206",
        "X-TrailForge-Origin-Secret",
        "TRAILFORGE_ORIGIN_SHARED_SECRET",
        "TRAILFORGE_ENABLE_DOCS",
        "Cloudflare Web Analytics",
    ):
        assert token in waf_rules, f"deploy/cloudflare/waf-rules.md missing {token!r}"

    # Strict privacy & repo hygiene scan across all owned deploy assets
    forbidden_patterns = (
        re.compile(r"/[U]sers/"),
        re.compile(r"/[h]ome/[a-zA-Z0-9._-]+/"),
        re.compile(r"[C]:\\[U]sers\\"),
        re.compile(r"g" + r"cp-dev", re.IGNORECASE),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"ghp_[0-9A-Za-z]{20,}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    )
    email_pattern = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    for rel in CLOUD_DEPLOY_FILES:
        file_path = REPO_ROOT / rel
        assert file_path.is_file(), f"Missing cloud deploy file: {rel}"
        text = file_path.read_text(encoding="utf-8")
        for pat in forbidden_patterns:
            assert pat.search(text) is None, f"Forbidden pattern {pat.pattern!r} found in {rel}"
        for email in email_pattern.findall(text):
            assert email.endswith("@example.com"), f"Non-placeholder email {email!r} found in {rel}"


@requires_repo_checkout
def test_hardening_env_parity_across_env_example_and_compose_files() -> None:
    env_example_path = REPO_ROOT / ".env.example"
    dev_compose_path = REPO_ROOT / "docker-compose.yml"
    ghcr_compose_path = REPO_ROOT / "docker-compose.ghcr.yml"

    env_example_text = env_example_path.read_text(encoding="utf-8")
    env_example = _parse_dotenv(env_example_path)
    dev_api_env = _compose_api_env(dev_compose_path)
    ghcr_api_env = _compose_api_env(ghcr_compose_path)

    for var in HARDENING_ENV_VARS:
        assert var in env_example, f".env.example missing {var!r}"
        assert var in dev_api_env, f"docker-compose.yml api service missing {var!r}"
        assert var in ghcr_api_env, f"docker-compose.ghcr.yml api service missing {var!r}"

    # Safe local defaults (open in dev; invite_only documented for public instances)
    assert env_example["TRAILFORGE_REGISTRATION_MODE"] == "open"
    assert dev_api_env["TRAILFORGE_REGISTRATION_MODE"] == "${TRAILFORGE_REGISTRATION_MODE:-open}"
    assert ghcr_api_env["TRAILFORGE_REGISTRATION_MODE"] == "${TRAILFORGE_REGISTRATION_MODE:-open}"
    assert env_example["TILES_PUBLIC_BASE_URL"] == ""
    assert env_example["PACKS_PUBLIC_BASE_URL"] == ""
    assert env_example["TRAILFORGE_RATE_LIMIT_ENABLED"] == "true"
    assert env_example["TRAILFORGE_UPLOADS_MAX_TOTAL_MB"] == "500"

    for label, text in (
        (".env.example", env_example_text),
        ("docker-compose.yml", dev_compose_path.read_text(encoding="utf-8")),
        ("docker-compose.ghcr.yml", ghcr_compose_path.read_text(encoding="utf-8")),
    ):
        assert "invite_only" in text, f"{label} should document 'invite_only' for public instances"


@requires_repo_checkout
def test_dockerfile_runs_non_root_and_defaults_install_dev_false() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG INSTALL_DEV=false" in dockerfile
    assert "ARG INSTALL_DEV=true" not in dockerfile
    assert "useradd" in dockerfile and "trailforge" in dockerfile
    assert "chown -R trailforge:trailforge /data /app" in dockerfile
    assert "\nUSER trailforge\n" in dockerfile

    # Dev compose overrides INSTALL_DEV=true so `make test-integration` retains pytest
    dev_compose = _load_yaml(REPO_ROOT / "docker-compose.yml")
    for svc_name in ("api", "worker"):
        build_args = dev_compose["services"][svc_name]["build"]["args"]
        assert str(build_args["INSTALL_DEV"]).lower() == "true"

    # Railway env template keeps docs disabled in production
    railway_env = _parse_dotenv(REPO_ROOT / "deploy" / "railway" / "env.railway.example")
    assert railway_env["TRAILFORGE_ENABLE_DOCS"] == "false"
    assert railway_env["TRAILFORGE_ORIGIN_SHARED_SECRET"] != ""
