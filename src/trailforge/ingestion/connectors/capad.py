"""DCCEEW CAPAD 2025 Protected Areas ArcGIS REST connector (CC BY 4.0)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import httpx

from trailforge.ingestion.connectors.base import (
    ATTRIBUTION_CAPAD,
    AU_STATE_CODES,
    BBox,
    RawCapadFeature,
    StateLiteral,
    iter_spatial_tiles,
    prop_ci,
    query_arcgis_geojson,
)

DEFAULT_AU_PARKS_URL: Final[str] = (
    "https://gis.environment.gov.au/gispubmap/rest/services/ogc_services/CAPAD/FeatureServer/0/query"
)
# ~5 m simplification offset keeps massive National Park / IPA polygons < 500 KB
CAPAD_MAX_ALLOWABLE_OFFSET_DEG: Final[str] = "0.00005"

_SUPPORTED_STATES: Final[frozenset[str]] = frozenset(AU_STATE_CODES)


def parse_capad_feature(feat: Mapping[str, Any]) -> RawCapadFeature | None:
    """Normalize a CAPAD 2025 GeoJSON feature into ``RawCapadFeature`` (all 8 AU states)."""
    geom = feat.get("geometry")
    if not isinstance(geom, Mapping) or geom.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    props = dict(feat.get("properties") or {})

    name = str(prop_ci(props, "NAME", "name") or "").strip()
    if not name:
        return None
    state_raw = str(prop_ci(props, "STATE", "state") or "").strip().upper()
    if state_raw not in _SUPPORTED_STATES:
        return None
    state: StateLiteral = state_raw  # type: ignore[assignment]

    park_type = str(prop_ci(props, "TYPE", "park_type") or "Protected Area").strip()
    type_abbr_val = prop_ci(props, "TYPE_ABBR", "type_abbr")
    iucn_val = prop_ci(props, "IUCN", "iucn")
    feat_id = str(
        prop_ci(props, "PA_ID", "pa_id", "OBJECTID", "objectid", "id")
        or feat.get("id")
        or f"CAPAD-{state}-{name}"
    ).strip()

    return RawCapadFeature(
        source_feature_id=feat_id,
        name=name,
        park_type=park_type,
        type_abbr=str(type_abbr_val).strip() if type_abbr_val is not None else None,
        iucn=str(iucn_val).strip() if iucn_val is not None else None,
        state=state,
        geometry=dict(geom),
    )


class CAPADConnector:
    """DCCEEW Collaborative Australian Protected Areas Database (CAPAD 2025) connector."""

    name: str = "au-dcceew-capad"
    attribution: str = ATTRIBUTION_CAPAD

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        cache_dir: Path | str | None = None,
        timeout_s: float = 25.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.endpoint = endpoint or os.environ.get("TRAILFORGE_AU_PARKS_URL", DEFAULT_AU_PARKS_URL)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout_s = timeout_s
        self._client = client

    def _cache_file(self, key: str) -> Path | None:
        return (self.cache_dir / "au_capad" / f"{key}.json") if self.cache_dir is not None else None

    def load_parks_geojson(self, path: Path | str) -> list[RawCapadFeature]:
        """Load CAPAD protected area polygons from a local GeoJSON file (100% offline)."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        out: list[RawCapadFeature] = []
        for feat in payload.get("features") or []:
            if isinstance(feat, Mapping):
                parsed = parse_capad_feature(feat)
                if parsed is not None:
                    out.append(parsed)
        return out

    def fetch_parks(self, bbox: BBox, *, tile_deg: float = 2.0) -> list[RawCapadFeature]:
        """Fetch simplified CAPAD 2025 protected areas intersecting ``bbox``."""
        parks: list[RawCapadFeature] = []
        seen: set[str] = set()
        for tile in iter_spatial_tiles(bbox.round_enclosing(), max_deg=tile_deg):
            bhash = tile.cache_hash()
            feats = query_arcgis_geojson(
                [self.endpoint],
                tile,
                cache_path=self._cache_file(f"capad_{bhash}"),
                out_fields="OBJECTID,PA_ID,NAME,TYPE,TYPE_ABBR,IUCN,STATE",
                extra_params={"maxAllowableOffset": CAPAD_MAX_ALLOWABLE_OFFSET_DEG},
                timeout_s=self.timeout_s,
                client=self._client,
            )
            for feat in feats:
                parsed = parse_capad_feature(feat)
                if parsed is not None and parsed.source_feature_id not in seen:
                    seen.add(parsed.source_feature_id)
                    parks.append(parsed)
        return parks
