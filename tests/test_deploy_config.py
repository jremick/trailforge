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
        "TRAILFORGE_VECTOR_BASEMAP_URL",
    ):
        assert required_secret in api_env
    auth_key = ghcr["services"]["api"]["environment"]["AUTH_SECRET_KEY"]
    assert auth_key.startswith("${AUTH_SECRET_KEY:?")
    privacy_key = ghcr["services"]["api"]["environment"]["TRAILFORGE_PRIVACY_SECRET"]
    assert privacy_key.startswith("${TRAILFORGE_PRIVACY_SECRET:?")
    worker_privacy = ghcr["services"]["worker"]["environment"]["TRAILFORGE_PRIVACY_SECRET"]
    assert worker_privacy.startswith("${TRAILFORGE_PRIVACY_SECRET:?")

    assert "TRAILFORGE_VECTOR_BASEMAP_URL=" in (REPO_ROOT / ".env.example").read_text(
        encoding="utf-8"
    )
    assert "TRAILFORGE_VECTOR_BASEMAP_URL=" in (
        REPO_ROOT / "deploy" / "railway" / "env.railway.example"
    ).read_text(encoding="utf-8")

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
    assert "packages" not in perms

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
        assert jobs[job_name]["permissions"] == {"contents": "read", "packages": "write"}
        steps = jobs[job_name]["steps"]
        for step in steps:
            if "uses" in step:
                assert re.fullmatch(r"[a-zA-Z0-9_./-]+@[a-f0-9]{40}", step["uses"])
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

    # Optional Railway instant redeploy job: stable releases only, secret-gated, pinned CLI
    assert jobs["resolve-tag"]["permissions"] == {}
    assert "deploy-railway" in jobs
    deploy_job = jobs["deploy-railway"]
    assert deploy_job["permissions"] == {}
    assert set(deploy_job["needs"]) == {"resolve-tag", "build-backend"}
    assert "needs.resolve-tag.outputs.is_stable == 'true'" in str(deploy_job["if"])
    assert len(deploy_job["steps"]) >= 2
    install_step, deploy_step = deploy_job["steps"][0], deploy_job["steps"][1]
    assert "RAILWAY_TOKEN" not in (install_step.get("env") or {})
    install_run = str(install_step["run"])
    assert "sha256sum -c -" in install_run
    assert "install.sh" not in install_run
    assert re.fullmatch(r"[a-f0-9]{64}", str(install_step["env"]["RAILWAY_CLI_SHA256"]))
    assert deploy_step["env"]["RAILWAY_TOKEN"] == "${{ secrets.RAILWAY_TOKEN }}"
    deploy_run = str(deploy_step["run"])
    assert 'if [ -z "${RAILWAY_TOKEN}" ]' in deploy_run
    assert "set -f" in deploy_run
    assert 'railway redeploy --service "${svc}" --from-source --yes' in deploy_run


@requires_repo_checkout
def test_build_ignore_files_exclude_private_gis_and_keep_fixtures() -> None:
    gcloudignore = (REPO_ROOT / ".gcloudignore").read_text(encoding="utf-8")
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "#!include:.gitignore" in gcloudignore
    for pattern in ("*.gpx", "*.geojson", "*.gpkg", "*.osm.pbf", "*.pmtiles"):
        assert pattern in dockerignore, f".dockerignore missing {pattern}"
    for keep in ("!tests/fixtures/**", "!examples/**", "!web/public/data/**"):
        assert keep in dockerignore, f".dockerignore missing fixture keep rule {keep}"


@requires_repo_checkout
def test_nginx_tile_and_pack_proxies_enforce_per_ip_limits_and_bounded_buffering() -> None:
    nginx = (REPO_ROOT / "web" / "nginx.conf").read_text(encoding="utf-8")
    assert "limit_conn_zone $server_name zone=tile_proxy_connections:1m;" in nginx
    assert "limit_conn_zone $binary_remote_addr zone=tile_proxy_per_ip:4m;" in nginx

    tiles_block = nginx.split("location /api/tiles/ {", 1)[1].split("location ~ ^/api/packs", 1)[0]
    assert "proxy_buffering on;" in tiles_block
    assert "proxy_max_temp_file_size 8m;" in tiles_block
    assert "limit_conn tile_proxy_per_ip 8;" in tiles_block
    assert "limit_conn tile_proxy_connections 24;" in tiles_block

    packs_block = nginx.split("location ~ ^/api/packs(/|$) {", 1)[1].split(
        "location = /api/oauth", 1
    )[0]
    assert "proxy_buffering on;" in packs_block
    assert "proxy_buffering off;" not in packs_block
    assert "proxy_max_temp_file_size 8m;" in packs_block
    assert "limit_conn tile_proxy_per_ip 4;" in packs_block
    assert "limit_conn tile_proxy_connections 24;" in packs_block

    mcp_block = nginx.split("location ~ ^/mcp(/|$) {", 1)[1].split(
        "location ~ ^/\\.well-known", 1
    )[0]
    assert "proxy_buffering off;" in mcp_block


@requires_repo_checkout
def test_download_proxy_trust_is_opt_in_and_packaged_for_supported_ingress() -> None:
    nginx = (REPO_ROOT / "web" / "nginx.conf").read_text(encoding="utf-8")
    dockerfile = (REPO_ROOT / "web" / "Dockerfile").read_text(encoding="utf-8")
    assert "real_ip_header X-Forwarded-For;" in nginx
    assert "real_ip_recursive off;" in nginx
    assert "set_real_ip_from" not in nginx
    assert "include /etc/nginx/trailforge-ingress/ingress.conf;" in nginx

    # Trust-boundary maps must only trust forwarded headers/scheme on the private Unix socket.
    assert 'map $http_x_forwarded_for $trailforge_tcp_forwarded_for {' in nginx
    assert 'default "$http_x_forwarded_for, $realip_remote_addr";' in nginx
    assert 'map $realip_remote_addr $trailforge_forwarded_for {' in nginx
    assert '"unix:" $remote_addr;' in nginx
    assert "default $trailforge_tcp_forwarded_for;" in nginx
    assert 'map "$realip_remote_addr|$http_x_forwarded_host" $trailforge_upstream_host {' in nginx
    assert '"~^unix:\\|(?<trailforge_socket_host>[A-Za-z0-9.-]+(?::[0-9]+)?)$"' in nginx
    assert 'map "$realip_remote_addr|$http_x_forwarded_proto" $trailforge_upstream_proto {' in nginx
    proto_map = nginx.split(
        'map "$realip_remote_addr|$http_x_forwarded_proto" $trailforge_upstream_proto {', 1
    )[1].split("}", 1)[0]
    assert "default $scheme;" in proto_map
    assert '"unix:|https" https;' in proto_map
    assert "~" not in proto_map

    # Every proxied download/API block must forward the normalized client chain.
    assert nginx.count("proxy_set_header X-Forwarded-For $trailforge_forwarded_for;") >= 6
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" not in nginx

    assert "/docker-entrypoint.d/19-trailforge-ingress.sh" in dockerfile
    assert dockerfile.index("USER root") < dockerfile.index("USER 101")
    assert "-m 700 -o 101 -g 101 /etc/nginx/trailforge-ingress" in dockerfile
    assert "-m 600 -o 101 -g 101 /dev/null /etc/nginx/trailforge-ingress/ingress.conf" in dockerfile

    for name in ("docker-compose.yml", "docker-compose.ghcr.yml"):
        web = _load_yaml(REPO_ROOT / name)["services"]["web"]
        assert web["ports"] == ["127.0.0.1:${WEB_PORT:-5173}:8080"]
        assert "TRAILFORGE_NGINX_PROXY_SOCKET" not in _env_keys(web)
        assert "TRAILFORGE_NGINX_TRUSTED_PROXY_IP" not in _env_keys(web)

    socket_web = _load_yaml(REPO_ROOT / "deploy" / "download-socket-compose.override.yml")[
        "services"
    ]["web"]
    assert socket_web["environment"] == {"TRAILFORGE_NGINX_PROXY_SOCKET": "true"}
    assert socket_web["volumes"][0]["target"] == "/run/trailforge-ingress"
    assert socket_web["volumes"][0]["bind"]["create_host_path"] is False

    traefik_path = REPO_ROOT / "deploy" / "traefik-compose.override.yml"
    traefik_raw = traefik_path.read_text(encoding="utf-8")
    usage_header = traefik_raw.split("# Prerequisites:", 1)[0]
    assert "TRAILFORGE_TRAEFIK_PROXY_IP=" in usage_header
    traefik = _load_yaml(traefik_path)
    traefik_ip = traefik["services"]["web"]["environment"]["TRAILFORGE_NGINX_TRUSTED_PROXY_IP"]
    assert traefik_ip.startswith("${TRAILFORGE_TRAEFIK_PROXY_IP:?")

    readme = (REPO_ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    upgrades_section = readme.split("## Low-Downtime Upgrades & Rollbacks", 1)[1]
    assert "TRAILFORGE_TRAEFIK_PROXY_IP" in upgrades_section


@requires_repo_checkout
def test_nginx_ingress_entrypoint_script_validation_and_socket_recovery(tmp_path: Path) -> None:
    import os
    import shutil
    import socket
    import subprocess
    import tempfile
    import threading

    script_src = REPO_ROOT / "web" / "nginx-ingress.sh"
    assert script_src.is_file()

    # Syntax-check the unmodified POSIX entrypoint script.
    syntax = subprocess.run(["sh", "-n", str(script_src)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    # Create a temp-path copy of the script (production keeps hardcoded paths with no env override).
    # Use a short temp path for socket_dir so AF_UNIX bind stays well under macOS's 104-byte cap.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="tf_sock_") as short_dir:
        config_file = tmp_path / "ingress.conf"
        config_file.write_text("", encoding="utf-8")
        socket_dir = Path(short_dir)
        os.chmod(socket_dir, 0o700)

        test_script = tmp_path / "nginx-ingress.test.sh"
        raw = script_src.read_text(encoding="utf-8")
        patched = raw.replace(
            "config=/etc/nginx/trailforge-ingress/ingress.conf", f"config={config_file}", 1
        ).replace(
            "socket_dir=/run/trailforge-ingress", f"socket_dir={socket_dir}", 1
        )
        assert patched != raw
        test_script.write_text(patched, encoding="utf-8")
        os.chmod(test_script, 0o755)

        # Provide a portable `stat -c '%a'` shim when running on BSD/macOS where stat lacks `-c`.
        shim_bin = tmp_path / "bin"
        shim_bin.mkdir()
        gnu_stat_check = subprocess.run(
            ["stat", "-c", "%a", str(socket_dir)], capture_output=True, text=True
        )
        if gnu_stat_check.returncode != 0:
            stat_shim = shim_bin / "stat"
            stat_shim.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-c" ] && [ "$2" = "%a" ]; then\n'
                '  exec /usr/bin/stat -f "%Lp" "$3"\n'
                "fi\n"
                'exec /usr/bin/stat "$@"\n',
                encoding="utf-8",
            )
            os.chmod(stat_shim, 0o755)

        def run_ingress(**env_overrides: str) -> subprocess.CompletedProcess[str]:
            env = {
                "PATH": f"{shim_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                **env_overrides,
            }
            return subprocess.run(
                ["sh", str(test_script)], env=env, capture_output=True, text=True
            )

        # 1. Default (disabled) leaves config empty; disabling later truncates stale config.
        assert run_ingress().returncode == 0
        assert config_file.read_text(encoding="utf-8") == ""

        res = run_ingress(TRAILFORGE_NGINX_PROXY_SOCKET="yes")
        assert res.returncode != 0

        # 2. Unsafe socket directory permissions must be rejected (0755, 0701, 0600).
        for bad_mode in (0o755, 0o701, 0o600):
            os.chmod(socket_dir, bad_mode)
            try:
                res = run_ingress(TRAILFORGE_NGINX_PROXY_SOCKET="true")
                assert res.returncode != 0, f"Mode {oct(bad_mode)} unexpectedly succeeded"
            finally:
                os.chmod(socket_dir, 0o700)

        # 3. Safe socket directory modes (0700, 0710) write the unix listener and real_ip directive.
        for ok_mode in (0o700, 0o710):
            os.chmod(socket_dir, ok_mode)
            res = run_ingress(TRAILFORGE_NGINX_PROXY_SOCKET="true")
            assert res.returncode == 0, res.stderr
            assert config_file.read_text(encoding="utf-8") == (
                f"listen unix:{socket_dir}/web.sock;\nset_real_ip_from unix:;\n"
            )
        os.chmod(socket_dir, 0o700)

        # Non-socket entry at web.sock is rejected.
        sock_path = socket_dir / "web.sock"
        sock_path.write_text("not a socket", encoding="utf-8")
        res = run_ingress(TRAILFORGE_NGINX_PROXY_SOCKET="true")
        assert res.returncode != 0
        sock_path.unlink()

        # Stale vs live Unix socket handling (when curl is available).
        if shutil.which("curl"):
            # Stale dead socket (bound then closed without unlink) is probed and cleaned up.
            stale_srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale_srv.bind(str(sock_path))
            stale_srv.close()
            assert sock_path.exists()
            res = run_ingress(TRAILFORGE_NGINX_PROXY_SOCKET="true")
            assert res.returncode == 0, res.stderr
            assert not sock_path.exists()

            # Live listening socket must NOT be unlinked and must cause startup failure.
            live_srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            live_srv.bind(str(sock_path))
            live_srv.listen(1)

            def _accept_once() -> None:
                try:
                    conn, _ = live_srv.accept()
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                    conn.close()
                except OSError:
                    pass

            t = threading.Thread(target=_accept_once, daemon=True)
            t.start()
            try:
                res = run_ingress(TRAILFORGE_NGINX_PROXY_SOCKET="true")
                assert res.returncode != 0
                assert "already in use" in res.stderr
                assert sock_path.exists()
            finally:
                live_srv.close()
                t.join(timeout=2)
                if sock_path.exists():
                    sock_path.unlink()

        # 4. Valid IPv4 and IPv6 literals produce exact /32 and /128 directives.
        for ip, expected_mask in (
            ("192.0.2.10", "32"),
            ("0.0.0.0", "32"),
            ("fd7a:115c:a1e0::1", "128"),
            ("::1", "128"),
            ("::ffff:192.0.2.10", "128"),
            ("2001:db8:1:2:3:4:5:6", "128"),
        ):
            res = run_ingress(TRAILFORGE_NGINX_TRUSTED_PROXY_IP=ip)
            assert res.returncode == 0, f"{ip} failed: {res.stderr}"
            assert (
                config_file.read_text(encoding="utf-8")
                == f"set_real_ip_from {ip}/{expected_mask};\n"
            )

        # Disabling both clears previously written ingress.conf.
        assert run_ingress().returncode == 0
        assert config_file.read_text(encoding="utf-8") == ""

        # 5. Malformed IPs, CIDRs, leading-zero octets, and injection payloads are rejected.
        for bad_ip in (
            "192.0.2.10/24",
            "999.999.999.999",
            "010.0.0.1",
            "....",
            "1.2.3.4.5",
            "1.2.3",
            ":",
            ":::",
            ":1",
            "1:",
            "1::2::3",
            "12345::1",
            "1:2:3",
            "1:2:3:4:5:6:7::8",
            "::ffff:999.0.0.1",
            "192.0.2.10; set_real_ip_from all",
            "all",
            "unix:",
        ):
            res = run_ingress(TRAILFORGE_NGINX_TRUSTED_PROXY_IP=bad_ip)
            assert res.returncode != 0, f"Malformed IP {bad_ip!r} unexpectedly passed"
            assert config_file.read_text(encoding="utf-8") == ""


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
    assert re.search(
        r"^\s*reverse_proxy unix//var/lib/trailforge-ingress/web\.sock \{", caddy, re.M
    )
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
