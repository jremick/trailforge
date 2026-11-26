"""Safe local photo attachment storage with magic-byte verification and strict path confinement."""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from trailforge.config import get_settings

MAX_PHOTO_BYTES: Final[int] = 5 * 1024 * 1024  # 5 MiB
DEFAULT_UPLOADS_DIR: Final[str] = "/tmp/trailforge_uploads"

_SAFE_FILENAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.(jpg|jpeg|png|webp)$"
)

_EXT_TO_MEDIA_TYPE: Final[dict[str, str]] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


class PhotoValidationError(ValueError):
    """Raised when an uploaded photo fails magic-byte or size validation."""


class PhotoPathTraversalError(ValueError):
    """Raised when a requested photo filename attempts directory traversal or invalid naming."""


class PhotoNotFoundError(FileNotFoundError):
    """Raised when a valid confined photo filename does not exist on disk."""


class PhotoStorageQuotaError(OSError):
    """Raised when saving a photo would exceed the global uploads_max_total_mb disk quota."""


@dataclass(frozen=True, slots=True)
class SavedPhoto:
    """Metadata returned after validating and persisting a report photo attachment."""

    filename: str
    media_type: str
    size_bytes: int
    url: str
    path: Path


def get_uploads_dir(override: Path | str | None = None) -> Path:
    """Resolve the report photo uploads directory safely across env and Settings."""
    if override is not None:
        return Path(override).expanduser()

    env_dir = os.environ.get("TRAILFORGE_UPLOADS_DIR") or os.environ.get("UPLOADS_DIR")
    if env_dir and env_dir.strip():
        return Path(env_dir.strip()).expanduser()

    settings_dir = getattr(get_settings(), "uploads_dir", None)
    if isinstance(settings_dir, str) and settings_dir.strip():
        return Path(settings_dir.strip()).expanduser()

    return Path(DEFAULT_UPLOADS_DIR)


def detect_image_format(data: bytes) -> tuple[str, str] | None:
    """Detect ``(ext, media_type)`` strictly from raw magic bytes (JPEG, PNG, WebP)."""
    if len(data) < 8:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return ("jpg", "image/jpeg")
    if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:4] == b"\x89PNG":
        return ("png", "image/png")
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("webp", "image/webp")
    return None


def validate_photo_bytes(data: bytes) -> tuple[str, str]:
    """Validate photo payload size (<= 5 MB) and image magic bytes, returning ``(ext, mime)``."""
    if not data:
        raise PhotoValidationError("Uploaded photo is empty")
    if len(data) > MAX_PHOTO_BYTES:
        raise PhotoValidationError(
            f"Photo exceeds maximum allowed size of {MAX_PHOTO_BYTES // (1024 * 1024)} MB"
        )
    detected = detect_image_format(data)
    if detected is None:
        raise PhotoValidationError(
            "Unsupported or invalid image file; only JPEG, PNG, and WebP images are accepted"
        )
    return detected


def get_directory_size_bytes(path: Path | str) -> int:
    """Return total byte size of regular files inside ``path`` (ignoring symlinks)."""
    root = Path(path).expanduser()
    if not root.is_dir():
        return 0
    total = 0
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += int(entry.stat(follow_symlinks=False).st_size)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def get_uploads_max_total_bytes(override_mb: int | float | None = None) -> int:
    """Resolve the global photo uploads disk quota in bytes."""
    if override_mb is not None:
        return max(0, int(float(override_mb) * 1024 * 1024))

    env_val = os.environ.get("TRAILFORGE_UPLOADS_MAX_TOTAL_MB") or os.environ.get(
        "UPLOADS_MAX_TOTAL_MB"
    )
    if env_val and env_val.strip():
        try:
            parsed_mb = float(env_val.strip())
            if parsed_mb > 0:
                return int(parsed_mb * 1024 * 1024)
        except ValueError:
            pass

    settings_mb = getattr(get_settings(), "uploads_max_total_mb", 500)
    return max(0, int(settings_mb) * 1024 * 1024)


def save_report_photo(
    data: bytes,
    *,
    uploads_dir: Path | str | None = None,
    max_total_bytes: int | None = None,
) -> SavedPhoto:
    """Validate and atomically save a photo attachment under the uploads directory."""
    ext, media_type = validate_photo_bytes(data)
    base_dir = get_uploads_dir(uploads_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    resolved_base = base_dir.resolve()

    quota_bytes = (
        max(0, int(max_total_bytes))
        if max_total_bytes is not None
        else get_uploads_max_total_bytes()
    )
    current_bytes = get_directory_size_bytes(resolved_base)
    if current_bytes + len(data) > quota_bytes:
        raise PhotoStorageQuotaError(
            "Photo upload storage quota exceeded "
            f"({current_bytes + len(data)} bytes would exceed {quota_bytes} byte limit)"
        )

    filename = f"{uuid.uuid4().hex}.{ext}"
    final_path = (resolved_base / filename).resolve()
    if not final_path.is_relative_to(resolved_base):
        raise PhotoPathTraversalError("Computed upload destination escaped uploads directory")

    tmp_path = resolved_base / f".{filename}.tmp"
    try:
        tmp_path.write_bytes(data)
        tmp_path.replace(final_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    return SavedPhoto(
        filename=filename,
        media_type=media_type,
        size_bytes=len(data),
        url=f"/api/reports/photos/{filename}",
        path=final_path,
    )


def resolve_photo_path(
    filename: str,
    *,
    uploads_dir: Path | str | None = None,
) -> tuple[Path, str]:
    """Resolve and strictly confine a photo filename inside the uploads directory."""
    if not filename or "\x00" in filename:
        raise PhotoPathTraversalError("Invalid photo filename")

    if "/" in filename or "\\" in filename or ".." in filename or ":" in filename:
        raise PhotoPathTraversalError("Invalid photo filename")

    match = _SAFE_FILENAME_RE.match(filename)
    if match is None:
        raise PhotoPathTraversalError("Invalid photo filename")

    ext = match.group(1).lower()
    media_type = _EXT_TO_MEDIA_TYPE[ext]

    base_dir = get_uploads_dir(uploads_dir)
    resolved_base = base_dir.resolve()
    raw_candidate = resolved_base / filename
    if raw_candidate.is_symlink():
        raise PhotoPathTraversalError("Symlink photo paths are forbidden")

    candidate = raw_candidate.resolve()
    if not candidate.is_relative_to(resolved_base):
        raise PhotoPathTraversalError("Photo path escapes uploads directory")

    if not candidate.is_file():
        raise PhotoNotFoundError(f"Photo not found: {filename}")

    return candidate, media_type
