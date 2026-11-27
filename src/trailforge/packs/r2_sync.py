"""Pure-Python AWS SigV4 + streaming httpx uploader for Cloudflare R2 tiles & offline packs.

Zero ``boto3`` dependency: signs S3-compatible requests with stdlib ``hmac`` and
``hashlib`` (AWS Signature Version 4, region ``auto``) and streams files in 64 KiB
chunks directly from disk so multi-gigabyte PMTiles archives upload in O(1) RAM.

Usage:
    python -W error::RuntimeWarning -m trailforge.packs.r2_sync \\
        --tiles-dir /data/tiles --packs-dir /data/packs [--region australian-alps] [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlsplit

import httpx

from trailforge.config import get_settings
from trailforge.packs.builder import STATIC_PACK_TELEMETRY_POLICY
from trailforge.tiles.builder import (
    PUBLIC_VECTOR_PMTILES_FILENAME,
    STATIC_TILE_TELEMETRY_POLICY,
    VECTOR_PMTILES_FILENAME,
)

EMPTY_PAYLOAD_SHA256: Final[str] = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)
UNSIGNED_PAYLOAD: Final[str] = "UNSIGNED-PAYLOAD"
DEFAULT_R2_REGION: Final[str] = "auto"
DEFAULT_R2_SERVICE: Final[str] = "s3"
_STREAM_CHUNK_BYTES: Final[int] = 65_536

TILE_ARTIFACTS: Final[tuple[tuple[str, str, str], ...]] = (
    (VECTOR_PMTILES_FILENAME, "application/vnd.pmtiles", "public, max-age=86400"),
    ("trailforge_terrain.pmtiles", "application/vnd.pmtiles", "public, max-age=86400"),
    ("manifest.json", "application/json", "public, max-age=300"),
)

PACK_ARTIFACTS: Final[tuple[tuple[str, str, str], ...]] = (
    ("manifest.json", "application/json", "no-store"),
    ("snapshot.json", "application/json", "no-store"),
    ("vector.pmtiles", "application/vnd.pmtiles", "no-store"),
    ("terrain.pmtiles", "application/vnd.pmtiles", "public, max-age=86400"),
    ("bundle.tar.gz", "application/gzip", "no-store"),
)


@dataclass(frozen=True, slots=True)
class R2SyncArtifact:
    """One local file queued for Cloudflare R2 synchronization."""

    local_path: Path
    object_key: str
    content_type: str
    cache_control: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class R2SyncItemResult:
    """Outcome for a single R2 sync artifact."""

    object_key: str
    local_path: str
    size_bytes: int
    content_type: str
    cache_control: str
    status: str  # "uploaded" | "skipped" | "dry_run"
    http_status: int | None = None


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _md5_file_hex(path: Path) -> str:
    """Stream MD5 digest (used exclusively for S3/R2 single-part ETag comparison)."""
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _iter_file_chunks(path: Path, chunk_size: int = _STREAM_CHUNK_BYTES) -> Iterator[bytes]:
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            yield chunk


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _build_object_url(endpoint_url: str, bucket: str, object_key: str) -> str:
    clean_endpoint = endpoint_url.strip().rstrip("/")
    clean_bucket = bucket.strip().strip("/")
    clean_key = object_key.strip().lstrip("/")
    encoded_key = quote(clean_key, safe="/-_.~")
    return f"{clean_endpoint}/{quote(clean_bucket, safe='-_.~')}/{encoded_key}"


def sign_s3_v4_request(
    *,
    method: str,
    url: str,
    access_key_id: str,
    secret_access_key: str,
    region: str = DEFAULT_R2_REGION,
    service: str = DEFAULT_R2_SERVICE,
    payload_hash: str = UNSIGNED_PAYLOAD,
    extra_headers: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Compute AWS Signature Version 4 headers for an S3/R2 HTTP request."""
    parsed = urlsplit(url)
    host = parsed.netloc
    canonical_uri = quote(parsed.path or "/", safe="/-_.~%")
    # Normalize any unencoded path segments while preserving already-encoded sequences
    if "%" not in (parsed.path or ""):
        canonical_uri = quote(parsed.path or "/", safe="/-_.~")

    canonical_querystring = ""
    if parsed.query:
        pairs: list[tuple[str, str]] = []
        for segment in parsed.query.split("&"):
            if "=" in segment:
                k, v = segment.split("=", 1)
            else:
                k, v = segment, ""
            pairs.append((quote(k, safe="-_.~"), quote(v, safe="-_.~")))
        pairs.sort()
        canonical_querystring = "&".join(f"{k}={v}" for k, v in pairs)

    ts = (now or datetime.now(UTC)).astimezone(UTC)
    amz_date = ts.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = ts.strftime("%Y%m%d")

    headers_to_sign: dict[str, str] = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    output_headers: dict[str, str] = {
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }

    if extra_headers:
        for raw_key, raw_val in extra_headers.items():
            clean_val = " ".join(str(raw_val).strip().split())
            lower_key = raw_key.strip().lower()
            headers_to_sign[lower_key] = clean_val
            output_headers[raw_key] = clean_val

    sorted_keys = sorted(headers_to_sign.keys())
    canonical_headers = "".join(f"{k}:{headers_to_sign[k]}\n" for k in sorted_keys)
    signed_headers = ";".join(sorted_keys)

    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_querystring,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ]
    )

    k_date = _hmac_sha256(f"AWS4{secret_access_key}".encode(), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    output_headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return output_headers


def _join_key(prefix: str, key: str) -> str:
    clean_prefix = prefix.strip().strip("/")
    clean_key = key.strip().lstrip("/")
    return f"{clean_prefix}/{clean_key}" if clean_prefix else clean_key


def _manifest_policy_is(path: Path, expected: str) -> bool:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(manifest, dict) and manifest.get("community_telemetry_policy") == expected
    except (OSError, ValueError):
        return False


def discover_sync_artifacts(
    *,
    tiles_dir: Path | str | None = None,
    packs_dir: Path | str | None = None,
    region: str | None = None,
    tiles_prefix: str = "",
    packs_prefix: str = "",
) -> list[R2SyncArtifact]:
    """Scan local tiles and regional pack directories for R2 upload artifacts."""
    artifacts: list[R2SyncArtifact] = []

    if tiles_dir is not None:
        tdir = Path(tiles_dir).expanduser()
        if tdir.is_dir():
            static_tiles_current = _manifest_policy_is(
                tdir / "manifest.json", STATIC_TILE_TELEMETRY_POLICY
            )
            for filename, content_type, cache_control in TILE_ARTIFACTS:
                key_filename = filename
                if (
                    filename in (VECTOR_PMTILES_FILENAME, "manifest.json")
                    and not static_tiles_current
                ):
                    continue
                if filename == VECTOR_PMTILES_FILENAME:
                    key_filename = PUBLIC_VECTOR_PMTILES_FILENAME
                fpath = tdir / filename
                if fpath.is_file():
                    artifacts.append(
                        R2SyncArtifact(
                            local_path=fpath,
                            object_key=_join_key(tiles_prefix, key_filename),
                            content_type=content_type,
                            cache_control=cache_control,
                            size_bytes=int(fpath.stat().st_size),
                        )
                    )

    if packs_dir is not None:
        pdir = Path(packs_dir).expanduser()
        if pdir.is_dir():
            if region:
                region_dirs = [pdir / region.strip()]
            else:
                region_dirs = sorted(
                    d for d in pdir.iterdir() if d.is_dir() and not d.name.startswith(".")
                )

            for rdir in region_dirs:
                if not rdir.is_dir():
                    continue
                rid = rdir.name
                current_policy = _manifest_policy_is(
                    rdir / "manifest.json", STATIC_PACK_TELEMETRY_POLICY
                )
                for filename, content_type, cache_control in PACK_ARTIFACTS:
                    if not current_policy and filename != "terrain.pmtiles":
                        continue
                    fpath = rdir / filename
                    if fpath.is_file():
                        artifacts.append(
                            R2SyncArtifact(
                                local_path=fpath,
                                object_key=_join_key(
                                    packs_prefix, f"{rid}/{STATIC_PACK_TELEMETRY_POLICY}/{filename}"
                                ),
                                content_type=content_type,
                                cache_control=cache_control,
                                size_bytes=int(fpath.stat().st_size),
                            )
                        )

    return artifacts


def _remote_matches_local(
    head_resp: httpx.Response,
    artifact: R2SyncArtifact,
) -> bool:
    """Return True when remote R2 object metadata proves the artifact is unchanged."""
    if head_resp.status_code != 200:
        return False
    if head_resp.headers.get("cache-control") != artifact.cache_control:
        return False

    raw_len = head_resp.headers.get("content-length")
    if raw_len is None:
        return False
    try:
        remote_size = int(raw_len.strip())
    except ValueError:
        return False
    if remote_size != artifact.size_bytes:
        return False

    raw_etag = (head_resp.headers.get("etag") or "").strip().strip('"').lower()
    if raw_etag and "-" not in raw_etag and len(raw_etag) == 32:
        return raw_etag == _md5_file_hex(artifact.local_path)
    return True


def sync_to_r2(
    *,
    tiles_dir: Path | str | None = None,
    packs_dir: Path | str | None = None,
    region: str | None = None,
    endpoint_url: str | None = None,
    bucket: str | None = None,
    access_key_id: str | None = None,
    secret_access_key: str | None = None,
    r2_region: str = DEFAULT_R2_REGION,
    tiles_prefix: str = "",
    packs_prefix: str = "",
    dry_run: bool = False,
    force: bool = False,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Sync local PMTiles and offline regional packs to Cloudflare R2 via SigV4 streaming PUT."""
    artifacts = discover_sync_artifacts(
        tiles_dir=tiles_dir,
        packs_dir=packs_dir,
        region=region,
        tiles_prefix=tiles_prefix,
        packs_prefix=packs_prefix,
    )

    results: list[R2SyncItemResult] = []
    if dry_run:
        for art in artifacts:
            results.append(
                R2SyncItemResult(
                    object_key=art.object_key,
                    local_path=str(art.local_path),
                    size_bytes=art.size_bytes,
                    content_type=art.content_type,
                    cache_control=art.cache_control,
                    status="dry_run",
                )
            )
        return _summarize_sync(results, dry_run=True, bucket=bucket or "")

    cfg = get_settings()
    resolved_endpoint = (endpoint_url if endpoint_url is not None else cfg.r2_endpoint_url).strip()
    resolved_bucket = (bucket if bucket is not None else cfg.r2_bucket).strip()
    resolved_key = (access_key_id if access_key_id is not None else cfg.r2_access_key_id).strip()
    resolved_secret = (
        secret_access_key if secret_access_key is not None else cfg.r2_secret_access_key
    ).strip()

    missing = [
        name
        for name, val in (
            ("TRAILFORGE_R2_ENDPOINT_URL", resolved_endpoint),
            ("TRAILFORGE_R2_BUCKET", resolved_bucket),
            ("TRAILFORGE_R2_ACCESS_KEY_ID", resolved_key),
            ("TRAILFORGE_R2_SECRET_ACCESS_KEY", resolved_secret),
        )
        if not val
    ]
    if missing:
        raise ValueError(f"Missing required Cloudflare R2 configuration: {', '.join(missing)}")

    owns_client = client is None
    http_client = client or httpx.Client(timeout=httpx.Timeout(30.0, write=300.0, read=60.0))

    try:
        for art in artifacts:
            obj_url = _build_object_url(resolved_endpoint, resolved_bucket, art.object_key)

            if not force:
                head_headers = sign_s3_v4_request(
                    method="HEAD",
                    url=obj_url,
                    access_key_id=resolved_key,
                    secret_access_key=resolved_secret,
                    region=r2_region,
                    payload_hash=EMPTY_PAYLOAD_SHA256,
                )
                head_resp = http_client.head(obj_url, headers=head_headers)
                if _remote_matches_local(head_resp, art):
                    results.append(
                        R2SyncItemResult(
                            object_key=art.object_key,
                            local_path=str(art.local_path),
                            size_bytes=art.size_bytes,
                            content_type=art.content_type,
                            cache_control=art.cache_control,
                            status="skipped",
                            http_status=head_resp.status_code,
                        )
                    )
                    continue

            put_headers = sign_s3_v4_request(
                method="PUT",
                url=obj_url,
                access_key_id=resolved_key,
                secret_access_key=resolved_secret,
                region=r2_region,
                payload_hash=UNSIGNED_PAYLOAD,
                extra_headers={
                    "Content-Type": art.content_type,
                    "Cache-Control": art.cache_control,
                    "Content-Length": str(art.size_bytes),
                },
            )
            put_resp = http_client.put(
                obj_url,
                headers=put_headers,
                content=_iter_file_chunks(art.local_path),
            )
            if put_resp.status_code not in (200, 201, 204):
                raise RuntimeError(
                    f"R2 upload failed for {art.object_key!r}: "
                    f"HTTP {put_resp.status_code} ({put_resp.text[:240]})"
                )
            results.append(
                R2SyncItemResult(
                    object_key=art.object_key,
                    local_path=str(art.local_path),
                    size_bytes=art.size_bytes,
                    content_type=art.content_type,
                    cache_control=art.cache_control,
                    status="uploaded",
                    http_status=put_resp.status_code,
                )
            )
    finally:
        if owns_client:
            http_client.close()

    return _summarize_sync(results, dry_run=False, bucket=resolved_bucket)


def _summarize_sync(
    items: list[R2SyncItemResult],
    *,
    dry_run: bool,
    bucket: str,
) -> dict[str, Any]:
    uploaded = sum(1 for item in items if item.status == "uploaded")
    skipped = sum(1 for item in items if item.status == "skipped")
    planned = sum(1 for item in items if item.status == "dry_run")
    total_bytes = sum(item.size_bytes for item in items)
    return {
        "dry_run": dry_run,
        "bucket": bucket,
        "total_files": len(items),
        "uploaded": uploaded,
        "skipped": skipped,
        "planned": planned,
        "total_bytes": total_bytes,
        "items": [
            {
                "object_key": item.object_key,
                "local_path": item.local_path,
                "size_bytes": item.size_bytes,
                "content_type": item.content_type,
                "cache_control": item.cache_control,
                "status": item.status,
                "http_status": item.http_status,
            }
            for item in items
        ],
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trailforge.packs.r2_sync",
        description="Sync TrailForge PMTiles and regional offline packs to Cloudflare R2 (SigV4).",
    )
    parser.add_argument(
        "--tiles-dir",
        default=None,
        help="Directory containing global PMTiles archives and manifest.json.",
    )
    parser.add_argument(
        "--packs-dir",
        default=None,
        help="Directory containing built <region_id>/ offline pack subdirectories.",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="Optional single regional pack ID to sync (e.g. australian-alps).",
    )
    parser.add_argument("--tiles-prefix", default="", help="Optional key prefix for tile files.")
    parser.add_argument("--packs-prefix", default="", help="Optional key prefix for pack files.")
    parser.add_argument("--endpoint-url", default=None, help="Override TRAILFORGE_R2_ENDPOINT_URL.")
    parser.add_argument("--bucket", default=None, help="Override TRAILFORGE_R2_BUCKET.")
    parser.add_argument("--access-key-id", default=None, help="Override R2 access key ID.")
    parser.add_argument("--secret-access-key", default=None, help="Override R2 secret key.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List discovered artifacts and target keys without performing network uploads.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Upload all files without skipping unchanged objects via HEAD check.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for Cloudflare R2 synchronization."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    cfg = get_settings()
    if args.tiles_dir is None and args.packs_dir is None:
        tiles_dir = None if args.region else cfg.tiles_dir
        packs_dir = cfg.packs_dir
    else:
        tiles_dir = args.tiles_dir
        packs_dir = args.packs_dir if args.packs_dir is not None else (
            cfg.packs_dir if args.region else None
        )

    try:
        summary = sync_to_r2(
            tiles_dir=tiles_dir,
            packs_dir=packs_dir,
            region=args.region,
            endpoint_url=args.endpoint_url,
            bucket=args.bucket,
            access_key_id=args.access_key_id,
            secret_access_key=args.secret_access_key,
            tiles_prefix=args.tiles_prefix,
            packs_prefix=args.packs_prefix,
            dry_run=args.dry_run,
            force=args.force,
        )
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
