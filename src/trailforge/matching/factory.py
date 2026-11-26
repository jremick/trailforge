"""Factory selecting the configured pluggable `TrackMatcher` backend."""

from __future__ import annotations

import httpx
from sqlalchemy.orm import Session

from trailforge.config import Settings, get_settings
from trailforge.matching.hmm import PostGISHMMMatcher
from trailforge.matching.protocol import TrackMatcher
from trailforge.matching.valhalla import ValhallaTrackMatcher


def get_track_matcher(
    settings: Settings | None = None,
    *,
    session: Session | None = None,
    http_client: httpx.Client | None = None,
) -> TrackMatcher:
    """Return the configured ``TrackMatcher`` (``PostGISHMMMatcher`` or ``ValhallaTrackMatcher``).

    Defaults to ``PostGISHMMMatcher`` when ``settings.matcher_backend == "postgis_hmm"``,
    and returns ``ValhallaTrackMatcher`` configured with graceful HMM fallback when
    ``settings.matcher_backend == "valhalla"``.
    """
    if isinstance(settings, Session):
        # Defensive support if a SQLAlchemy Session is passed as the first positional argument
        session = settings
        settings = None

    cfg = settings if settings is not None else get_settings()
    if cfg.matcher_backend == "valhalla":
        return ValhallaTrackMatcher(
            session=session,
            valhalla_url=cfg.valhalla_url,
            timeout_s=cfg.valhalla_timeout_s,
            fallback_to_hmm=cfg.valhalla_fallback_to_hmm,
            http_client=http_client,
        )

    return PostGISHMMMatcher(session)  # type: ignore[arg-type]
