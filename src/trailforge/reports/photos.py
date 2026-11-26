"""Safe local photo attachment storage with Pillow EXIF stripping and strict path confinement."""

from __future__ import annotations

import io
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

from trailforge.config import get_settings

MAX_PHOTO_BYTES: Final[int] = 5 * 1024 * 1024  # 5 MiB
MAX_PHOTO_DIMENSION: Final[int] = 4096
MAX_PHOTO_PIXELS: Final[int] = MAX_PHOTO_DIMENSION * MAX_PHOTO_DIMENSION
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

_EXT_TO_PIL_FORMAT: Final[dict[str, str]] = {
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "png": "PNG",
    "webp": "WEBP",
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


def _open_and_validate_image(data: bytes) -> tuple[Image.Image, str, str, str]:
    """Validate size, magic bytes, image integrity, and dimensions using Pillow."""
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
    ext, media_type = detected
    expected_format = _EXT_TO_PIL_FORMAT[ext]

    try:
        img = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise PhotoValidationError(
            "Corrupt or unreadable image file; only valid JPEG, PNG, and WebP images are accepted"
        ) from exc

    try:
        actual_format = (img.format or "").upper()
        if actual_format != expected_format:
            raise PhotoValidationError(
                "Unsupported or mismatched image format; only JPEG, PNG, and WebP are accepted"
            )
        width, height = img.size
        if width <= 0 or height <= 0:
            raise PhotoValidationError("Invalid image dimensions")
        if (
            width > MAX_PHOTO_DIMENSION
            or height > MAX_PHOTO_DIMENSION
            or (width * height) > MAX_PHOTO_PIXELS
        ):
            raise PhotoValidationError(
                f"Image dimensions ({width}x{height}) exceed maximum allowed "
                f"{MAX_PHOTO_DIMENSION}x{MAX_PHOTO_DIMENSION}"
            )
        img.load()
    except PhotoValidationError:
        img.close()
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
        SyntaxError,
    ) as exc:
        img.close()
        raise PhotoValidationError(
            "Corrupt or unreadable image file; only valid JPEG, PNG, and WebP images are accepted"
        ) from exc

    return img, expected_format, ext, media_type


def sanitize_photo_bytes(data: bytes) -> tuple[bytes, str, str]:
    """Decode, orient, validate dimensions, and re-encode photo stripping all EXIF/XMP/IPTC."""
    img, pil_format, ext, media_type = _open_and_validate_image(data)
    transposed: Image.Image | None = None
    converted: Image.Image | None = None
    clean: Image.Image | None = None
    try:
        transposed = ImageOps.exif_transpose(img)
        width, height = transposed.size
        if (
            width <= 0
            or height <= 0
            or width > MAX_PHOTO_DIMENSION
            or height > MAX_PHOTO_DIMENSION
            or (width * height) > MAX_PHOTO_PIXELS
        ):
            raise PhotoValidationError(
                f"Image dimensions ({width}x{height}) exceed maximum allowed "
                f"{MAX_PHOTO_DIMENSION}x{MAX_PHOTO_DIMENSION}"
            )

        has_alpha = transposed.mode in ("RGBA", "LA", "PA") or (
            transposed.mode == "P" and "transparency" in transposed.info
        )
        if pil_format == "JPEG":
            if has_alpha:
                rgba = transposed.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.getchannel("A"))
                rgba.close()
                converted = bg
            else:
                converted = (
                    transposed if transposed.mode == "RGB" else transposed.convert("RGB")
                )
            clean = Image.frombytes("RGB", converted.size, converted.tobytes())
            out = io.BytesIO()
            clean.save(out, format="JPEG", quality=88, optimize=True)
        else:
            target_mode = "RGBA" if has_alpha else "RGB"
            converted = (
                transposed
                if transposed.mode == target_mode
                else transposed.convert(target_mode)
            )
            clean = Image.frombytes(target_mode, converted.size, converted.tobytes())
            out = io.BytesIO()
            if pil_format == "PNG":
                clean.save(out, format="PNG", optimize=True)
            else:
                clean.save(out, format="WEBP", quality=85, method=4)

        sanitized = out.getvalue()
    except PhotoValidationError:
        raise
    except (OSError, ValueError) as exc:
        raise PhotoValidationError("Failed to sanitize uploaded image") from exc
    finally:
        if clean is not None:
            clean.close()
        if converted is not None and converted is not transposed and converted is not img:
            converted.close()
        if transposed is not None and transposed is not img:
            transposed.close()
        img.close()

    if not sanitized or len(sanitized) > MAX_PHOTO_BYTES:
        raise PhotoValidationError(
            f"Sanitized photo exceeds maximum allowed size of {MAX_PHOTO_BYTES // (1024 * 1024)} MB"
        )
    return sanitized, ext, media_type


def validate_photo_bytes(data: bytes) -> tuple[str, str]:
    """Validate photo payload size (<= 5 MB), real image decoding, and <= 4096x4096 bounds."""
    img, _pil_format, ext, media_type = _open_and_validate_image(data)
    img.close()
    return ext, media_type


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
    """Validate, EXIF-strip, and atomically save a photo attachment under the uploads directory."""
    clean_bytes, ext, media_type = sanitize_photo_bytes(data)
    base_dir = get_uploads_dir(uploads_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    resolved_base = base_dir.resolve()

    quota_bytes = (
        max(0, int(max_total_bytes))
        if max_total_bytes is not None
        else get_uploads_max_total_bytes()
    )
    current_bytes = get_directory_size_bytes(resolved_base)
    if current_bytes + len(clean_bytes) > quota_bytes:
        raise PhotoStorageQuotaError(
            "Photo upload storage quota exceeded "
            f"({current_bytes + len(clean_bytes)} bytes would exceed {quota_bytes} byte limit)"
        )

    filename = f"{uuid.uuid4().hex}.{ext}"
    final_path = (resolved_base / filename).resolve()
    if not final_path.is_relative_to(resolved_base):
        raise PhotoPathTraversalError("Computed upload destination escaped uploads directory")

    tmp_path = resolved_base / f".{filename}.tmp"
    try:
        tmp_path.write_bytes(clean_bytes)
        tmp_path.replace(final_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    return SavedPhoto(
        filename=filename,
        media_type=media_type,
        size_bytes=len(clean_bytes),
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
