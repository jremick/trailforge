"""Validation, normalization, and persistence helpers for UI layer defaults & user preferences."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from trailforge.config import get_settings
from trailforge.db.models import InstanceUiSettings, User

# Canonical default sidebar layer order (maps 1:1 with frontend `LayerKey` values).
CANONICAL_LAYER_KEYS: Final[tuple[str, ...]] = (
    "contoursHillshade",
    "parksWaterways",
    "pois",
    "tracks",
    "steepHighlights",
    "hotspots",
    "closures",
    "reports",
    "unmapped",
    "snowLine",
)

CANONICAL_LAYER_KEY_SET: Final[frozenset[str]] = frozenset(CANONICAL_LAYER_KEYS)

DEFAULT_LAYER_VISIBILITY: Final[dict[str, bool]] = {
    "contoursHillshade": True,
    "parksWaterways": True,
    "pois": True,
    "tracks": True,
    "steepHighlights": True,
    "hotspots": True,
    "closures": True,
    "reports": True,
    "unmapped": False,
    "snowLine": False,
}

_UNSET: Final[object] = object()


def _iso_or_none(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    aware = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_layer_order(raw: Any) -> list[str]:
    """Strictly validate an incoming layer ordering list, appending any omitted canonical keys."""
    if not isinstance(raw, (list, tuple)):
        raise ValueError("layer_order must be a list of canonical layer keys")

    seen: set[str] = set()
    ordered: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or entry not in CANONICAL_LAYER_KEY_SET:
            raise ValueError(f"Unknown map layer key: {entry!r}")
        if entry in seen:
            raise ValueError(f"Duplicate map layer key: {entry!r}")
        seen.add(entry)
        ordered.append(entry)

    if not ordered:
        raise ValueError("layer_order must contain at least one canonical layer key")

    for key in CANONICAL_LAYER_KEYS:
        if key not in seen:
            ordered.append(key)
    return ordered


def validate_layer_visibility(
    raw: Any,
    *,
    base: Mapping[str, bool] | None = None,
    merge_with_base: bool = False,
) -> dict[str, bool]:
    """Strictly validate a map of canonical layer keys to booleans."""
    if not isinstance(raw, Mapping):
        raise ValueError("layer_visibility must be an object mapping layer keys to booleans")

    cleaned: dict[str, bool] = dict(base) if (merge_with_base and base is not None) else {}
    for key, value in raw.items():
        if not isinstance(key, str) or key not in CANONICAL_LAYER_KEY_SET:
            raise ValueError(f"Unknown map layer key: {key!r}")
        if not isinstance(value, bool):
            raise ValueError(f"Visibility value for {key!r} must be a boolean")
        cleaned[key] = value
    return cleaned


def normalize_layer_order(
    raw: Any,
    *,
    fallback: Sequence[str] = CANONICAL_LAYER_KEYS,
) -> list[str]:
    """Leniently normalize a stored layer order, always returning all 10 canonical keys."""
    ordered: list[str] = []
    seen: set[str] = set()
    if isinstance(raw, (list, tuple)):
        for entry in raw:
            if isinstance(entry, str) and entry in CANONICAL_LAYER_KEY_SET and entry not in seen:
                seen.add(entry)
                ordered.append(entry)
    for key in fallback:
        if key in CANONICAL_LAYER_KEY_SET and key not in seen:
            seen.add(key)
            ordered.append(key)
    for key in CANONICAL_LAYER_KEYS:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def normalize_layer_visibility(
    raw: Any,
    *,
    fallback: Mapping[str, bool] = DEFAULT_LAYER_VISIBILITY,
) -> dict[str, bool]:
    """Leniently normalize stored layer visibilities against a complete fallback map."""
    result: dict[str, bool] = {
        key: bool(fallback.get(key, DEFAULT_LAYER_VISIBILITY[key])) for key in CANONICAL_LAYER_KEYS
    }
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if key in CANONICAL_LAYER_KEY_SET and isinstance(value, bool):
                result[key] = value
    return result


def get_public_ui_config(db: Session | None) -> dict[str, Any]:
    """Resolve effective public instance UI defaults (DB override -> env -> code defaults)."""
    cfg = get_settings()
    env_telemetry_public = bool(cfg.show_stack_telemetry_public)

    row: InstanceUiSettings | None = None
    if db is not None:
        try:
            row = db.get(InstanceUiSettings, 1)
        except SQLAlchemyError:
            # Fail open to env + factory defaults if DB is unreachable or pre-migration.
            row = None

    order = normalize_layer_order(row.default_layer_order if row else None)
    visibility = normalize_layer_visibility(row.default_layer_visibility if row else None)
    show_telemetry = (
        bool(row.show_stack_telemetry_public)
        if (row is not None and row.show_stack_telemetry_public is not None)
        else env_telemetry_public
    )

    return {
        "default_layer_order": order,
        "default_layer_visibility": visibility,
        "show_stack_telemetry_public": show_telemetry,
    }


def get_admin_ui_settings(db: Session) -> dict[str, Any]:
    """Return effective instance UI settings plus admin/env provenance metadata."""
    cfg = get_settings()
    env_telemetry_public = bool(cfg.show_stack_telemetry_public)
    row = db.get(InstanceUiSettings, 1)

    order = normalize_layer_order(row.default_layer_order if row else None)
    visibility = normalize_layer_visibility(row.default_layer_visibility if row else None)
    telemetry_override = row.show_stack_telemetry_public if row is not None else None
    effective_telemetry = (
        bool(telemetry_override) if telemetry_override is not None else env_telemetry_public
    )

    return {
        "default_layer_order": order,
        "default_layer_visibility": visibility,
        "show_stack_telemetry_public": effective_telemetry,
        "show_stack_telemetry_public_override": telemetry_override,
        "env_show_stack_telemetry_public": env_telemetry_public,
        "has_custom_layer_order": bool(row and row.default_layer_order is not None),
        "has_custom_layer_visibility": bool(row and row.default_layer_visibility is not None),
        "updated_at": _iso_or_none(row.updated_at if row else None),
    }


def upsert_instance_ui_settings(
    db: Session,
    *,
    default_layer_order: Any = _UNSET,
    default_layer_visibility: Any = _UNSET,
    show_stack_telemetry_public: Any = _UNSET,
    updated_by_user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Create or update singleton row ``id = 1`` in ``instance_ui_settings``."""
    row = db.get(InstanceUiSettings, 1)
    if row is None:
        row = InstanceUiSettings(id=1)
        db.add(row)

    if default_layer_order is not _UNSET:
        row.default_layer_order = (
            None if default_layer_order is None else validate_layer_order(default_layer_order)
        )

    if default_layer_visibility is not _UNSET:
        if default_layer_visibility is None:
            row.default_layer_visibility = None
        else:
            existing_base = normalize_layer_visibility(row.default_layer_visibility)
            row.default_layer_visibility = validate_layer_visibility(
                default_layer_visibility,
                base=existing_base,
                merge_with_base=True,
            )

    if show_stack_telemetry_public is not _UNSET:
        if show_stack_telemetry_public is None:
            row.show_stack_telemetry_public = None
        elif isinstance(show_stack_telemetry_public, bool):
            row.show_stack_telemetry_public = show_stack_telemetry_public
        else:
            raise ValueError("show_stack_telemetry_public must be a boolean or null")

    row.updated_by_user_id = updated_by_user_id
    row.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(row)
    return get_admin_ui_settings(db)


def reset_instance_ui_settings(db: Session) -> dict[str, Any]:
    """Delete admin UI setting overrides so factory & env defaults take effect."""
    row = db.get(InstanceUiSettings, 1)
    if row is not None:
        db.delete(row)
        db.commit()
    return get_admin_ui_settings(db)


def get_user_ui_preferences(user: User) -> dict[str, Any]:
    """Read and normalize saved UI preferences for an authenticated user."""
    raw = user.ui_preferences if isinstance(user.ui_preferences, Mapping) else {}
    raw_order = raw.get("layer_order")
    raw_vis = raw.get("layer_visibility")

    layer_order = normalize_layer_order(raw_order) if isinstance(raw_order, list) else None
    layer_visibility: dict[str, bool] | None = None
    if isinstance(raw_vis, Mapping):
        cleaned = {
            k: v
            for k, v in raw_vis.items()
            if isinstance(k, str) and k in CANONICAL_LAYER_KEY_SET and isinstance(v, bool)
        }
        if cleaned:
            layer_visibility = cleaned

    return {
        "layer_order": layer_order,
        "layer_visibility": layer_visibility,
        "has_custom_preferences": bool(layer_order is not None or layer_visibility is not None),
    }


def update_user_ui_preferences(
    db: Session,
    user: User,
    *,
    layer_order: Any = _UNSET,
    layer_visibility: Any = _UNSET,
) -> dict[str, Any]:
    """Persist validated personal layer order and/or default visibility for ``user``."""
    current = dict(user.ui_preferences) if isinstance(user.ui_preferences, Mapping) else {}

    if layer_order is not _UNSET:
        if layer_order is None:
            current.pop("layer_order", None)
        else:
            current["layer_order"] = validate_layer_order(layer_order)

    if layer_visibility is not _UNSET:
        if layer_visibility is None:
            current.pop("layer_visibility", None)
        else:
            existing_vis = (
                current.get("layer_visibility")
                if isinstance(current.get("layer_visibility"), Mapping)
                else {}
            )
            validated = validate_layer_visibility(layer_visibility)
            merged = {
                k: v
                for k, v in existing_vis.items()
                if isinstance(k, str) and k in CANONICAL_LAYER_KEY_SET and isinstance(v, bool)
            }
            merged.update(validated)
            if merged:
                current["layer_visibility"] = merged
            else:
                current.pop("layer_visibility", None)

    user.ui_preferences = current
    user.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    return get_user_ui_preferences(user)


def reset_user_ui_preferences(db: Session, user: User) -> dict[str, Any]:
    """Clear all saved personal UI preferences for ``user``."""
    user.ui_preferences = {}
    user.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    return get_user_ui_preferences(user)
