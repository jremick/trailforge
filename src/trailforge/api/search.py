"""Unified trigram-indexed search API for tracks, POIs, CAPAD parks, and AU geocoder fallback."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import UnboundExecutionError
from sqlalchemy.orm import Session

from trailforge.api.geocoder import geocode_au_places
from trailforge.config import get_settings
from trailforge.db.models import POI, CAPADPark, Track, TrackSegment, TrackSegmentGrade
from trailforge.db.session import get_db

router = APIRouter(prefix="/api", tags=["search"])

_RANK_GRADE = {1: "Easy", 2: "Medium", 3: "Hard", 4: "Extreme"}
_SEARCH_CACHE_MAX_ENTRIES = 256
_SEARCH_CACHE_TTL_SECONDS = 30.0
_SEARCH_POI_SUBQUERY_CAP = 5
_SEARCH_PARK_SUBQUERY_CAP = 4
_SEARCH_KIND_ORDER = {"track": 0, "park": 1, "poi": 2, "place": 3}

_search_cache_lock = threading.Lock()
_search_cache: OrderedDict[tuple[int, str, int, bool], tuple[float, SearchResponse]] = OrderedDict()


class SearchResultItem(BaseModel):
    kind: str
    id: str
    name: str
    state: str
    subtitle: str | None = None
    category: str | None = None
    grade: str | None = None
    lon: float | None = None
    lat: float | None = None
    bbox: list[float] | None = None


class SearchResponse(BaseModel):
    query: str
    total: int
    results: list[SearchResultItem]


def _escape_like(value: str) -> str:
    """Escape SQL LIKE/ILIKE metacharacters (`\\`, `%`, `_`) for literal substring matching."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def reset_search_cache() -> None:
    """Clear the bounded in-process ``/api/search`` LRU + TTL cache."""
    with _search_cache_lock:
        _search_cache.clear()


def _db_cache_scope(db: Session) -> int:
    """Derive a stable per-engine cache scope so isolated test engines never collide."""
    get_bind = getattr(db, "get_bind", None)
    if callable(get_bind):
        try:
            bind = get_bind()
        except (UnboundExecutionError, AttributeError, TypeError):
            return id(db)
        if bind is not None:
            engine = getattr(bind, "engine", bind)
            return id(engine)
    return id(db)


def _search_cache_get(key: tuple[int, str, int, bool]) -> SearchResponse | None:
    now = time.monotonic()
    with _search_cache_lock:
        entry = _search_cache.get(key)
        if entry is None:
            return None
        cached_at, payload = entry
        if now - cached_at > _SEARCH_CACHE_TTL_SECONDS:
            _search_cache.pop(key, None)
            return None
        _search_cache.move_to_end(key)
        return payload.model_copy(deep=True)


def _search_cache_put(key: tuple[int, str, int, bool], response: SearchResponse) -> None:
    now = time.monotonic()
    snapshot = response.model_copy(deep=True)
    with _search_cache_lock:
        _search_cache[key] = (now, snapshot)
        _search_cache.move_to_end(key)
        while len(_search_cache) > _SEARCH_CACHE_MAX_ENTRIES:
            _search_cache.popitem(last=False)


def _bbox_midpoint(
    xmin: float | None, ymin: float | None, xmax: float | None, ymax: float | None
) -> tuple[float | None, float | None]:
    if xmin is None or ymin is None or xmax is None or ymax is None:
        return (None, None)
    return (
        round((float(xmin) + float(xmax)) / 2.0, 6),
        round((float(ymin) + float(ymax)) / 2.0, 6),
    )


def _bbox_list(
    xmin: float | None, ymin: float | None, xmax: float | None, ymax: float | None
) -> list[float] | None:
    if xmin is None or ymin is None or xmax is None or ymax is None:
        return None
    return [
        round(float(xmin), 6),
        round(float(ymin), 6),
        round(float(xmax), 6),
        round(float(ymax), 6),
    ]


def _search_match_tier(name: str, term_lower: str) -> int:
    low = name.strip().lower()
    if low == term_lower:
        return 0
    if low.startswith(term_lower):
        return 1
    if term_lower in low:
        return 2
    return 3


def _merge_search_results(
    track_items: list[SearchResultItem],
    poi_items: list[SearchResultItem],
    park_items: list[SearchResultItem],
    *,
    term: str,
    limit: int,
) -> list[SearchResultItem]:
    """Budget slots so tracks lead while matching parks & POIs still surface cleanly."""
    if limit <= 0:
        return []

    term_lower = term.lower()
    indexed_tracks = list(enumerate(track_items))
    indexed_parks = list(enumerate(park_items))
    indexed_pois = list(enumerate(poi_items))

    selected: list[tuple[int, int, int, SearchResultItem]] = []
    selected_keys: set[tuple[str, str]] = set()

    def _try_add(item: SearchResultItem, orig_idx: int) -> bool:
        if len(selected) >= limit:
            return False
        key = (item.kind, item.id)
        if key in selected_keys:
            return False
        selected_keys.add(key)
        tier = _search_match_tier(item.name, term_lower)
        kind_ord = _SEARCH_KIND_ORDER.get(item.kind, 9)
        selected.append((tier, kind_ord, orig_idx, item))
        return True

    for bucket in (indexed_tracks, indexed_parks, indexed_pois):
        for idx, item in bucket:
            if _search_match_tier(item.name, term_lower) == 0:
                _try_add(item, idx)

    if len(selected) < limit:
        remaining_budget = limit - len(selected)
        rem_tracks = [
            (idx, item) for idx, item in indexed_tracks if (item.kind, item.id) not in selected_keys
        ]
        rem_parks = [
            (idx, item) for idx, item in indexed_parks if (item.kind, item.id) not in selected_keys
        ]
        rem_pois = [
            (idx, item) for idx, item in indexed_pois if (item.kind, item.id) not in selected_keys
        ]

        if remaining_budget >= 3:
            park_reserve = min(len(rem_parks), min(2, max(1, remaining_budget // 8)))
            poi_reserve = min(len(rem_pois), min(3, max(1, remaining_budget // 6)))
            track_target = max(1, remaining_budget - park_reserve - poi_reserve)
            for idx, item in rem_tracks[:track_target]:
                _try_add(item, idx)
            for idx, item in rem_parks[:park_reserve]:
                _try_add(item, idx)
            for idx, item in rem_pois[:poi_reserve]:
                _try_add(item, idx)
        else:
            for bucket in (rem_tracks, rem_parks, rem_pois):
                for idx, item in bucket:
                    if _search_match_tier(item.name, term_lower) == 1:
                        _try_add(item, idx)

        for bucket in (rem_tracks, rem_parks, rem_pois):
            for idx, item in bucket:
                if len(selected) >= limit:
                    break
                _try_add(item, idx)

    selected.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    return [entry[3] for entry in selected[:limit]]


def _token_match_col(col: Any, tokens: list[str], pattern: str) -> Any:
    if len(tokens) <= 1:
        return col.ilike(pattern, escape="\\")
    return and_(*(col.ilike(f"%{tok}%", escape="\\") for tok in tokens))


def _name_relevance_rank(col: Any, term_lower: str, prefix_pattern: str, pattern: str) -> Any:
    return case(
        (func.lower(col) == term_lower, 0),
        (col.ilike(prefix_pattern, escape="\\"), 1),
        (col.ilike(pattern, escape="\\"), 2),
        else_=3,
    )


def _revalidate_cached_response(db: Session, cached: SearchResponse) -> SearchResponse | None:
    cached_tracks = [item for item in cached.results if item.kind == "track"]
    if not cached_tracks:
        return cached
    current_grades = {
        row.track_id: row.effective_grade
        for row in db.execute(
            select(Track.track_id, Track.effective_grade).where(
                Track.track_id.in_([item.id for item in cached_tracks])
            )
        ).all()
    }
    if all(current_grades.get(item.id) for item in cached_tracks):
        for item in cached_tracks:
            item.grade = current_grades[item.id]
        return cached
    return None


def _query_search_tracks(
    db: Session,
    *,
    tokens: list[str],
    term_lower: str,
    prefix_pattern: str,
    pattern: str,
    limit: int,
) -> list[SearchResultItem]:
    relevance_rank = _name_relevance_rank(Track.name, term_lower, prefix_pattern, pattern)
    class_rank = case(
        (Track.trail_class == "4wd_track", 0),
        (Track.trail_class == "4wd_route", 0),
        (Track.trail_class == "unsealed_road", 1),
        else_=2,
    )
    track_rows = db.execute(
        select(
            Track.track_id,
            Track.name,
            Track.state,
            Track.trail_class,
            Track.effective_grade,
            func.ST_XMin(Track.bbox_geom).label("xmin"),
            func.ST_YMin(Track.bbox_geom).label("ymin"),
            func.ST_XMax(Track.bbox_geom).label("xmax"),
            func.ST_YMax(Track.bbox_geom).label("ymax"),
        )
        .where(
            Track.is_active.is_(True),
            _token_match_col(Track.search_text, tokens, pattern),
        )
        .order_by(
            relevance_rank.asc(),
            class_rank.asc(),
            func.length(Track.name).asc(),
            Track.name.asc(),
        )
        .limit(limit)
    ).all()

    missing_grade_ids = [r.track_id for r in track_rows if not r.effective_grade]
    fallback_rank_by_track: dict[str, int] = {}
    if missing_grade_ids:
        grade_rank_expr = case(
            (TrackSegmentGrade.effective_grade == "Easy", 1),
            (TrackSegmentGrade.effective_grade == "Medium", 2),
            (TrackSegmentGrade.effective_grade == "Hard", 3),
            (TrackSegmentGrade.effective_grade == "Extreme", 4),
            else_=0,
        )
        for tid, worst_rank in db.execute(
            select(
                TrackSegment.track_id,
                func.coalesce(func.max(grade_rank_expr), 0),
            )
            .outerjoin(TrackSegmentGrade, TrackSegment.segment_id == TrackSegmentGrade.segment_id)
            .where(
                TrackSegment.is_active.is_(True),
                TrackSegment.track_id.in_(missing_grade_ids),
            )
            .group_by(TrackSegment.track_id)
        ).all():
            if tid is not None:
                fallback_rank_by_track[tid] = int(worst_rank or 0)

    items: list[SearchResultItem] = []
    for tid, name, state, trail_class, cached_grade, xmin, ymin, xmax, ymax in track_rows:
        eff_grade = cached_grade
        if not eff_grade:
            worst_rank = fallback_rank_by_track.get(tid, 0)
            eff_grade = _RANK_GRADE.get(worst_rank) if worst_rank else None
        lon, lat = _bbox_midpoint(xmin, ymin, xmax, ymax)
        items.append(
            SearchResultItem(
                kind="track",
                id=tid,
                name=name,
                state=state,
                subtitle=trail_class.replace("_", " "),
                category=trail_class,
                grade=eff_grade,
                lon=lon,
                lat=lat,
                bbox=_bbox_list(xmin, ymin, xmax, ymax),
            )
        )
    return items


def _query_search_pois(
    db: Session,
    *,
    tokens: list[str],
    term_lower: str,
    prefix_pattern: str,
    pattern: str,
    limit: int,
) -> list[SearchResultItem]:
    poi_rows = db.execute(
        select(
            POI.id,
            POI.name,
            POI.state,
            POI.poi_type,
            func.ST_X(POI.geom).label("lon"),
            func.ST_Y(POI.geom).label("lat"),
        )
        .where(_token_match_col(POI.name, tokens, pattern))
        .order_by(
            _name_relevance_rank(POI.name, term_lower, prefix_pattern, pattern).asc(),
            func.length(POI.name).asc(),
            POI.name.asc(),
        )
        .limit(min(limit, _SEARCH_POI_SUBQUERY_CAP))
    ).all()
    return [
        SearchResultItem(
            kind="poi",
            id=str(pid),
            name=name,
            state=state,
            subtitle=poi_type,
            category=poi_type,
            grade=None,
            lon=round(float(lon), 6),
            lat=round(float(lat), 6),
            bbox=None,
        )
        for pid, name, state, poi_type, lon, lat in poi_rows
    ]


def _query_search_parks(
    db: Session,
    *,
    tokens: list[str],
    term_lower: str,
    prefix_pattern: str,
    pattern: str,
    limit: int,
) -> list[SearchResultItem]:
    park_rows = db.execute(
        select(
            CAPADPark.id,
            CAPADPark.name,
            CAPADPark.state,
            CAPADPark.park_type,
            func.ST_XMin(CAPADPark.geom).label("xmin"),
            func.ST_YMin(CAPADPark.geom).label("ymin"),
            func.ST_XMax(CAPADPark.geom).label("xmax"),
            func.ST_YMax(CAPADPark.geom).label("ymax"),
        )
        .where(_token_match_col(CAPADPark.name, tokens, pattern))
        .order_by(
            _name_relevance_rank(CAPADPark.name, term_lower, prefix_pattern, pattern).asc(),
            func.length(CAPADPark.name).asc(),
            CAPADPark.name.asc(),
        )
        .limit(min(limit, _SEARCH_PARK_SUBQUERY_CAP))
    ).all()
    items: list[SearchResultItem] = []
    for pk_id, name, state, park_type, xmin, ymin, xmax, ymax in park_rows:
        lon, lat = _bbox_midpoint(xmin, ymin, xmax, ymax)
        items.append(
            SearchResultItem(
                kind="park",
                id=str(pk_id),
                name=name,
                state=state,
                subtitle=park_type,
                category=park_type,
                grade=None,
                lon=lon,
                lat=lat,
                bbox=_bbox_list(xmin, ymin, xmax, ymax),
            )
        )
    return items


@router.get("/search", response_model=SearchResponse)
def search_network(
    q: str = Query(..., min_length=1, max_length=120),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> SearchResponse:
    """Search canonical tracks (name & aliases), POIs, and CAPAD parks with coordinates/bbox."""
    term = q.strip()
    if not term:
        return SearchResponse(query="", total=0, results=[])

    term_lower = term.lower()
    geocoder_enabled = bool(get_settings().enable_geocoder)
    cache_key = (_db_cache_scope(db), term_lower, limit, geocoder_enabled)
    cached = _search_cache_get(cache_key)
    if cached is not None:
        revalidated = _revalidate_cached_response(db, cached)
        if revalidated is not None:
            return revalidated

    escaped_term = _escape_like(term)
    pattern = f"%{escaped_term}%"
    prefix_pattern = f"{escaped_term}%"
    tokens = [_escape_like(tok) for tok in term.split() if tok][:6]

    query_kwargs = {
        "tokens": tokens,
        "term_lower": term_lower,
        "prefix_pattern": prefix_pattern,
        "pattern": pattern,
        "limit": limit,
    }
    track_items = _query_search_tracks(db, **query_kwargs)
    poi_items = _query_search_pois(db, **query_kwargs)
    park_items = _query_search_parks(db, **query_kwargs)

    items = _merge_search_results(
        track_items,
        poi_items,
        park_items,
        term=term,
        limit=limit,
    )

    if not items and len(term) >= 3 and geocoder_enabled:
        seen_names: set[str] = set()
        remaining = max(1, min(5, limit))
        for place in geocode_au_places(term, limit=remaining):
            lowered_name = place.name.strip().lower()
            if lowered_name in seen_names:
                continue
            seen_names.add(lowered_name)
            items.append(
                SearchResultItem(
                    kind="place",
                    id=place.id,
                    name=place.name,
                    state=place.state,
                    subtitle=place.subtitle,
                    category=place.category,
                    grade=None,
                    lon=place.lon,
                    lat=place.lat,
                    bbox=place.bbox,
                )
            )

    trimmed = items[:limit]
    response = SearchResponse(query=term, total=len(trimmed), results=trimmed)
    _search_cache_put(cache_key, response)
    return response
