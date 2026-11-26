"""Public UI defaults config and authenticated per-user UI preferences API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from trailforge.auth.dependencies import get_current_user
from trailforge.db.models import User
from trailforge.db.session import get_db
from trailforge.ui_preferences import (
    get_public_ui_config,
    get_user_ui_preferences,
    reset_user_ui_preferences,
    update_user_ui_preferences,
)

router = APIRouter(prefix="/api", tags=["preferences"])


class PublicUiConfigResponse(BaseModel):
    default_layer_order: list[str]
    default_layer_visibility: dict[str, bool]
    show_stack_telemetry_public: bool


class UserUiPreferencesResponse(BaseModel):
    layer_order: list[str] | None = None
    layer_visibility: dict[str, bool] | None = None
    has_custom_preferences: bool = False


class UserUiPreferencesUpdateRequest(BaseModel):
    layer_order: list[str] | None = None
    layer_visibility: dict[str, bool] | None = None


@router.get("/ui-config", response_model=PublicUiConfigResponse)
def read_public_ui_config(db: Session = Depends(get_db)) -> PublicUiConfigResponse:
    """Return effective public default layer order, visibility, and sidebar telemetry visibility."""
    return PublicUiConfigResponse(**get_public_ui_config(db))


@router.get("/user/preferences", response_model=UserUiPreferencesResponse)
def read_user_preferences(
    user: User = Depends(get_current_user),
) -> UserUiPreferencesResponse:
    """Return the signed-in user's saved map layer preferences."""
    return UserUiPreferencesResponse(**get_user_ui_preferences(user))


@router.put("/user/preferences", response_model=UserUiPreferencesResponse)
def write_user_preferences(
    payload: UserUiPreferencesUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserUiPreferencesResponse:
    """Save the signed-in user's preferred map layer order and/or on/off defaults."""
    kwargs: dict[str, Any] = {}
    if "layer_order" in payload.model_fields_set:
        kwargs["layer_order"] = payload.layer_order
    if "layer_visibility" in payload.model_fields_set:
        kwargs["layer_visibility"] = payload.layer_visibility

    try:
        updated = update_user_ui_preferences(db, user, **kwargs)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return UserUiPreferencesResponse(**updated)


@router.delete("/user/preferences", response_model=UserUiPreferencesResponse)
def clear_user_preferences(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserUiPreferencesResponse:
    """Clear the signed-in user's custom map layer preferences to revert to instance defaults."""
    return UserUiPreferencesResponse(**reset_user_ui_preferences(db, user))
