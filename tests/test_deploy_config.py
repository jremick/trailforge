"""Offline parity & isolation tests for GHCR release workflow, Compose, deploy kit, and test DB."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Engine, text

from tests.conftest import DEFAULT_TEST_DATABASE_URL, resolve_test_database_url
from trailforge.config import get_settings
from trailforge.db.session import get_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
IN_REPO_CHECKOUT = (REPO_ROOT / "docker-compose.yml").is_file()

requires_repo_checkout = pytest.mark.skipif(
    not IN_REPO_CHECKOUT,
    reason="Repo-root deployment manifests are not copied into the minimal runtime container image",
)


class _ComposeSafeLoader(yaml.SafeLoader):
    """YAML SafeLoader supporting Docker Compose override tags like `!reset`."""


def _compose_reset_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeSafeLoader.add_constructor("!reset", _compose_reset_constructor)


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeSafeLoader)
    assert isinstance(data, dict), f"Expected top-level YAML mapping in {path}"
    return data


def _env_keys(service_cfg: dict[str, Any]) -> set[str]:
    env = service_cfg.get("environment") or {}
    if isinstance(env, dict):
        return set(env.keys())
    if isinstance(env, list):
        return {str(item).split("=", 1)[0] for item in env}
    return set()


@requires_repo_checkout
def test_ghcr_compose_parity_with_dev_compose() -> None:
    dev = _load_yaml(REPO_ROOT / "docker-compose.yml")
    ghcr = _load_yaml(REPO_ROOT / "docker-compose.ghcr.yml")

    # 1. Exact same 4 services, zero local build sections in GHCR compose
    assert set(ghcr["services"].keys()) == set(dev["services"].keys()) == {
        "db",
        "api",
        "worker",
        "web",
    }
    for svc_name, svc_cfg in ghcr["services"].items():
        assert "build" not in svc_cfg, f"GHCR service {svc_name!r} must not require local build"

    # 2. Expected multi-arch GHCR + PostGIS images
    assert ghcr["services"]["db"]["image"] == "${POSTGIS_IMAGE:-imresamu/postgis:16-3.4}"
    assert (
        ghcr["services"]["api"]["image"]
        == "ghcr.io/jremick/trailforge-backend:${TRAILFORGE_IMAGE_TAG:-latest}"
    )
    assert (
        ghcr["services"]["worker"]["image"]
        == "ghcr.io/jremick/trailforge-backend:${TRAILFORGE_IMAGE_TAG:-latest}"
    )
    assert (
        ghcr["services"]["web"]["image"]
        == "ghcr.io/jremick/trailforge-web:${TRAILFORGE_IMAGE_TAG:-latest}"
    )

    # 3. Safe loopback binding for reverse-proxy deployments (unprivileged Nginx on 8080)
    assert ghcr["services"]["web"]["ports"] == ["127.0.0.1:${WEB_PORT:-5173}:8080"]
    assert "ports" not in ghcr["services"]["db"]
    assert "ports" not in ghcr["services"]["api"]

    # 4. Full environment variable parity + mandatory production secrets/toggles
    for svc_name in ("db", "api", "worker"):
        dev_keys = _env_keys(dev["services"][svc_name])
        ghcr_keys = _env_keys(ghcr["services"][svc_name])
        missing = dev_keys - ghcr_keys
        assert not missing, f"GHCR service {svc_name!r} missing env vars: {sorted(missing)}"

    api_env = _env_keys(ghcr["services"]["api"])
    for required_secret in (
        "AUTH_SECRET_KEY",
        "TRAILFORGE_PRIVACY_SECRET",
        "AUTH_COOKIE_SECURE",
        "TRAILFORGE_ENABLE_LIVE_FEEDS",
        "CORS_ORIGINS",
    ):
        assert required_secret in api_env

    # 5. All 5 persistent volumes & per-service mounts match dev compose
    expected_volumes = {
        "trailforge-pgdata",
        "trailforge-data",
        "trailforge_tiles",
        "trailforge_packs",
        "trailforge_uploads",
    }
    assert set(ghcr["volumes"].keys()) == set(dev["volumes"].keys()) == expected_volumes
    for svc_name in ("db", "api", "worker"):
        assert ghcr["services"][svc_name]["volumes"] == dev["services"][svc_name]["volumes"]

    # 6. Lean homelab memory limits, healthchecks, depends_on & restart policies match
    expected_mem = {"db": "512m", "api": "512m", "worker": "512m", "web": "128m"}
    for svc_name, limit in expected_mem.items():
        assert ghcr["services"][svc_name]["mem_limit"] == limit
        assert ghcr["services"][svc_name]["mem_limit"] == dev["services"][svc_name]["mem_limit"]
        assert ghcr["services"][svc_name]["healthcheck"] == dev["services"][svc_name]["healthcheck"]
        assert ghcr["services"][svc_name]["restart"] == "unless-stopped"
        assert ghcr["services"][svc_name].get("depends_on") == dev["services"][svc_name].get(
            "depends_on"
        )


@requires_repo_checkout
def test_release_images_workflow_multiarch_oci_and_prerelease_guard() -> None:
    wf_path = REPO_ROOT / ".github" / "workflows" / "release-images.yml"
    assert wf_path.is_file(), "Missing .github/workflows/release-images.yml"
    wf = _load_yaml(wf_path)

    # PyYAML 1.1 parses unquoted `on:` as boolean True
    triggers = wf.get("on") or wf.get(True)
    assert isinstance(triggers, dict)
    assert "v*" in triggers["push"]["tags"]
    dispatch_inputs = triggers["workflow_dispatch"]["inputs"]
    assert "tag" in dispatch_inputs
    assert dispatch_inputs["tag"].get("required") is False

    perms = wf.get("permissions", {})
    assert perms.get("contents") == "read"
    assert perms.get("packages") == "write"

    jobs = wf["jobs"]
    assert {"resolve-tag", "build-backend", "build-web"}.issubset(jobs.keys())

    # Validate semver prerelease guard regex from resolve-tag job
    resolve_run = jobs["resolve-tag"]["steps"][0]["run"]
    regex_match = re.search(r'=~\s*(\^v\S+)\s*\]\]', resolve_run)
    assert regex_match is not None, "Expected semver validation regex in resolve-tag job"
    semver_re = re.compile(regex_match.group(1))
    assert semver_re.match("v1.0.0-beta.1") is not None
    assert semver_re.match("v1.0.0-beta.1").group(4) == "-beta.1"  # type: ignore[union-attr]
    assert semver_re.match("v1.0.0-rc.2").group(4) == "-rc.2"  # type: ignore[union-attr]
    assert semver_re.match("v1.0.0") is not None
    assert semver_re.match("v1.0.0").group(4) is None  # type: ignore[union-attr]
    assert semver_re.match("not-a-tag") is None
    assert 'IS_STABLE="false"' in resolve_run

    expected_jobs = {
        "build-backend": "ghcr.io/${{ github.repository_owner }}/trailforge-backend",
        "build-web": "ghcr.io/${{ github.repository_owner }}/trailforge-web",
    }
    for job_name, expected_image in expected_jobs.items():
        steps = jobs[job_name]["steps"]
        uses_list = [str(s.get("uses", "")) for s in steps]
        for action in (
            "docker/setup-qemu-action",
            "docker/setup-buildx-action",
            "docker/login-action",
            "docker/metadata-action",
            "docker/build-push-action",
        ):
            assert any(u.startswith(action) for u in uses_list), (
                f"Job {job_name!r} missing required action {action}"
            )

        meta_step = next(
            s for s in steps if str(s.get("uses", "")).startswith("docker/metadata-action")
        )
        meta_with = meta_step["with"]
        assert meta_with["images"] == expected_image
        tags_text = str(meta_with["tags"])
        assert "value=latest,enable=${{ needs.resolve-tag.outputs.is_stable }}" in tags_text

        labels_text = str(meta_with["labels"])
        assert "org.opencontainers.image.licenses=GPL-3.0-or-later" in labels_text
        assert "org.opencontainers.image.source=" in labels_text
        assert "org.opencontainers.image.title=" in labels_text
        assert "org.opencontainers.image.description=" in labels_text

        build_step = next(
            s for s in steps if str(s.get("uses", "")).startswith("docker/build-push-action")
        )
        build_with = build_step["with"]
        assert build_with["platforms"] == "linux/amd64,linux/arm64"
        assert build_with["push"] is True
        assert "type=gha" in str(build_with["cache-from"])
        assert "type=gha" in str(build_with["cache-to"])


@requires_repo_checkout
def test_deploy_kit_files_https_range_and_upload_limits() -> None:
    deploy_dir = REPO_ROOT / "deploy"
    caddy_path = deploy_dir / "Caddyfile.example"
    traefik_path = deploy_dir / "traefik-compose.override.yml"
    tailscale_path = deploy_dir / "tailscale-serve.md"
    readme_path = deploy_dir / "README.md"

    for p in (caddy_path, traefik_path, tailscale_path, readme_path):
        assert p.is_file(), f"Missing production deploy file: {p.relative_to(REPO_ROOT)}"

    # Caddy v2 checks
    caddy = caddy_path.read_text(encoding="utf-8")
    assert "127.0.0.1:5173" in caddy
    assert "max_size 17MB" in caddy
    assert "X-Frame-Options \"DENY\"" in caddy
    assert "header_up -CF-Connecting-IP" in caddy
    assert "X-Forwarded-Proto" in caddy
    assert "Host" in caddy
    assert "Range" in caddy and "206" in caddy
    assert "flush_interval -1" in caddy
    assert "/api/tiles/*" in caddy

    # Traefik v3 override checks
    traefik_raw = traefik_path.read_text(encoding="utf-8")
    traefik = _load_yaml(traefik_path)
    assert "traefik" in traefik["networks"]
    assert traefik["networks"]["traefik"]["external"] is True
    assert traefik["services"]["web"]["ports"] == []
    labels = "\n".join(traefik["services"]["web"]["labels"])
    assert "traefik.enable=true" in labels
    assert "entrypoints=websecure" in labels
    assert "tls=true" in labels
    assert "maxRequestBodyBytes=17825792" in labels
    assert "customFrameOptionsValue=DENY" in labels
    assert "customRequestHeaders.CF-Connecting-IP=" in labels
    assert "compress" not in labels
    assert "Range" in traefik_raw and "206" in traefik_raw
    assert "AUTH_COOKIE_SECURE" in traefik["services"]["api"]["environment"]

    # Tailscale Serve guide checks
    tailscale = tailscale_path.read_text(encoding="utf-8")
    assert "tailscale serve --bg http://127.0.0.1:5173" in tailscale
    assert "Service Worker" in tailscale
    assert "Geolocation" in tailscale
    assert "Screen Wake Lock" in tailscale
    assert "AUTH_COOKIE_SECURE=true" in tailscale
    assert "Range" in tailscale and "206" in tailscale

    # Production homelab checklist checks
    readme = readme_path.read_text(encoding="utf-8")
    assert "openssl rand -hex 32" in readme
    assert "AUTH_COOKIE_SECURE=true" in readme
    assert "TRAILFORGE_ENABLE_LIVE_FEEDS=true" in readme
    assert "pg_dump" in readme
    assert "/data/uploads" in readme
    assert "/data/packs" in readme
    assert "docker-compose.ghcr.yml" in readme


@requires_repo_checkout
def test_makefile_isolated_test_db_and_bake_targets() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "TEST_DB ?= trailforge_test" in makefile
    assert "CREATE DATABASE" in makefile
    assert "CREATE EXTENSION IF NOT EXISTS postgis" in makefile
    assert "alembic upgrade head" in makefile
    assert "TEST_DATABASE_URL" in makefile
    assert 'pytest -m "not live"' in makefile
    assert "bake-alps:" in makefile
    assert "bake-blue-mountains:" in makefile
    assert "--region australian-alps" in makefile
    assert "--region blue-mountains" in makefile

    # Confirm CI workflows remain compatible with TEST_DATABASE_URL contract
    ci_text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    cb_text = (REPO_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")
    assert "TEST_DATABASE_URL" in ci_text
    assert "TEST_DATABASE_URL" in cb_text


def test_conftest_isolated_test_database_url_resolution() -> None:
    # 1. Explicit TEST_DATABASE_URL takes precedence over dev DATABASE_URL
    assert (
        resolve_test_database_url(
            {
                "DATABASE_URL": "postgresql+psycopg://trailforge:trailforge@db:5432/trailforge",
                "TEST_DATABASE_URL": "postgresql+psycopg://trailforge:trailforge@db:5432/trailforge_test",
            }
        )
        == "postgresql+psycopg://trailforge:trailforge@db:5432/trailforge_test"
    )

    # 2. Falls back to DATABASE_URL when TEST_DATABASE_URL is unset/blank (CI compatibility)
    assert (
        resolve_test_database_url(
            {
                "DATABASE_URL": "postgresql+psycopg://trailforge:trailforge@localhost:5432/ci_db",
                "TEST_DATABASE_URL": "",
            }
        )
        == "postgresql+psycopg://trailforge:trailforge@localhost:5432/ci_db"
    )

    # 3. Defaults to isolated trailforge_test (never the live dev trailforge DB)
    resolved_default = resolve_test_database_url({})
    assert resolved_default == DEFAULT_TEST_DATABASE_URL
    assert resolved_default.endswith("/trailforge_test")


@pytest.mark.integration
def test_postgis_integration_engine_isolated_and_synced(postgis_engine: Engine) -> None:
    """Verify postgis_engine, get_settings(), and get_engine() all target the same test DB."""
    with postgis_engine.connect() as conn:
        fixture_db = conn.execute(text("SELECT current_database()")).scalar_one()

    with get_engine().connect() as conn:
        default_engine_db = conn.execute(text("SELECT current_database()")).scalar_one()

    assert fixture_db == default_engine_db
    assert get_settings().database_url == os.environ["DATABASE_URL"]
    expected_test_url = os.environ.get("TEST_DATABASE_URL")
    if expected_test_url:
        assert expected_test_url.rsplit("/", 1)[-1] == fixture_db
