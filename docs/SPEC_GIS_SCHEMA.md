# TrailForge GIS Connectors, Spatial Schema & Test Fixture Specification

> **Status:** Locked v1 Architectural Specification (Slices 01–09)  
> **Code License:** `GPL-3.0-or-later`  
> **Launch Coverage:** Victoria & New South Wales (including ACT), Australia  
> **Runtime Constraint:** Zero PyQGIS (`qgis.core`) dependency — PostGIS + GDAL + `osmium` + Tippecanoe + `rio-rgbify` → `PMTiles`

---

## 1. Reusable Australian Data Connectors (Audited from GridForge)

TrailForge adapts the verified, production-tested Australian open-data connectors audited from sibling project GridForge (`/Users/jarel/projects/oss-mapper`, read-only reference). Every connector supports an air-gapped local file override (`local_gpkg` / `local_geojson` / `local_dem` / `local_pbf`) so CI and offline packs run with zero network calls.

### 1.1 Coverage Bounds & v1 Regional Pack Presets (`EPSG:4326`)

| Region / Coverage ID | Scope | `min_lon` | `min_lat` | `max_lon` | `max_lat` | UTM / MGRS Zones |
| :--- | :--- | ---: | ---: | ---: | ---: | :--- |
| `au-coverage` | National DEM guard | `112.50` | `-44.50` | `154.50` | `-9.50` | `49–56S` |
| `vic-coverage` | VIC connector fast-path | `140.50` | `-39.40` | `150.20` | `-33.80` | `54H`, `55H` |
| `nsw-coverage` | NSW/ACT connector fast-path | `140.50` | `-37.80` | `154.00` | `-28.00` | `54H–56J` |
| `australian-alps` | Tactical v1 pack (VIC/NSW) | `147.80` | `-37.10` | `149.10` | `-35.80` | `55H` |
| `blue-mountains` | Tactical v1 pack (NSW) | `150.00` | `-34.05` | `150.70` | `-33.40` | `56H` |
| `act` | Territory pack (ACT) | `148.74` | `-35.94` | `149.42` | `-35.10` | `55H` |
| `vic` | State v1 pack (VIC) | `140.95` | `-39.20` | `150.05` | `-33.95` | `54H`, `55H` |
| `nsw` | State v1 pack (NSW) | `140.95` | `-37.55` | `153.68` | `-28.15` | `54H`, `55H`, `56H`, `56J` |

### 1.2 Verified Vector & Raster Upstream Endpoints

#### A. OpenStreetMap (Geofabrik Australia PBF)
- **Endpoint:** `https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf` (env override `TRAILFORGE_AU_PBF_URL`)
- **Streaming Extraction:**
  ```bash
  osmium extract -b <min_lon>,<min_lat>,<max_lon>,<max_lat> \
    --strategy=complete_ways <src.osm.pbf> -o <dst.osm.pbf> --overwrite
  ```
- **CRS:** `EPSG:4326`
- **Relevant Tags Extracted into `source_tracks_osm`, `source_waterways`, `source_pois`:**
  - Tracks/Roads: `highway IN ('track','path','unclassified','service','tertiary','ford')`, `name`, `alt_name`, `surface`, `tracktype` (`grade1`–`grade5`), `smoothness`, `4wd_only`, `access`, `motor_vehicle`, `vehicle`, `foot`, `bridge`, `ford`, `seasonal`, `incline`, `operator`
  - Gates/Barriers (node or way tags): `barrier IN ('gate','lift_gate','bollard','chain','cattle_grid','swing_gate')`, `locked`, `access`
  - Waterways: `waterway IN ('river','stream','creek','drain')`, `intermittent`, `name`
  - POIs: `tourism IN ('camp_site','alpine_hut','wilderness_hut','viewpoint','picnic_site')`, `amenity IN ('shelter','drinking_water','fuel')`, `man_made='survey_point'`, `ele`, `name`

#### B. NSW Spatial Services (DCS) — ArcGIS REST (`CC BY 4.0`)
All ArcGIS REST queries send `geometry=<min_x>,<min_y>,<max_x>,<max_y>`, `geometryType=esriGeometryEnvelope`, `inSR=4326`, `spatialRel=esriSpatialRelIntersects`, `outSR=4326`, `f=geojson` (with automatic fallback to `f=json` converting Esri `x/y`, `paths`, `rings` to GeoJSON).

| Dataset | Verified Endpoint & Layer ID | `where` Filter | `outFields` | Target Table & Field Mapping |
| :--- | :--- | :--- | :--- | :--- |
| **NSW Fire Trails** | `https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Transport_Theme/MapServer/9/query` | `1=1` | `*` | `source_tracks_nsw`: `trail_class='fire_trail'`; name from `roadname`/`firetrailname`/`trailname`/`name` → `roadnamebase + roadnametype` → `"Fire Trail {brimsid}"`; surface `1→sealed`, `2→unsealed`, `3→natural`, `4→unsealed` |
| **NSW Vehicular / Walking Tracks** | `https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Transport_Theme/MapServer/5/query` | `classsubtype IN (5, 6)` | `classsubtype,roadnamebase,roadnametype,functionhierarchy,surface,operationalstatus` | `source_tracks_nsw`: name containing `fire trail`/`firetrail`/ending ` ft` → `fire_trail`; `management trail`/`track` → `management_trail`; `classsubtype=6` → `walking_track` (`is_vehicular=FALSE`); `classsubtype=5` → `4wd_track` (`is_vehicular=TRUE`) |
| **NSW Trig Stations** | `https://portal.spatial.nsw.gov.au/server/rest/services/SurveyMarkGDA2020/MapServer/0/query` | `marktype='TS'` | `marktype,marknumber,trigname,trigtype,ahdheight_label,markstatus,monumenttype` | `source_pois`: `poi_type='trig'`, name=`trigname` or `"TS {marknumber}"`, `elev_m` parsed from `ahdheight_label`/`ahdheight` |
| **NSW POIs** | `https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_POI/MapServer/0/query` | `1=1` | `*` | `source_pois`: `poitype` classified to `hut`/`campground`/`lookout`/`picnic` (or regex `\b(hut\|huts\|bivouac\|shelter\|refuge)\b` on label) |
| **NSW Backcountry Huts** | `https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Features_of_Interest_Category/MapServer/3/query` | `UPPER(generalname) LIKE '%HUT%'` | `*` | `source_pois`: `poi_type='hut'`, name=`generalname` |

#### C. Victoria DataVic / DEECA / DTP Vicmap — OGC WFS 2.0.0 (`CC BY 4.0`)
- **Endpoint:** `https://opendata.maps.vic.gov.au/geoserver/wfs`
- **Standard Parameters:** `service=WFS`, `version=2.0.0`, `request=GetFeature`, `outputFormat=application/json`, `srsName=EPSG:4326`, `count=2500`, `CQL_FILTER=BBOX(geom,<min_x>,<min_y>,<max_x>,<max_y>,'EPSG:4326') [AND (<filter>)]`

| Dataset | `typeNames` | Additional `CQL_FILTER` | Field Mapping & Classification |
| :--- | :--- | :--- | :--- |
| **Vicmap Roads / 4WD & Fire Tracks** | `open-data-platform:tr_road` | `class_code IN (7,8,9) OR (class_code=6 AND road_seal='2')` | `source_tracks_vic`: name=`ezi_road_name_label`/`road_name`; `road_seal`: `1→sealed`, `2→unsealed`, `3→natural`; fire keywords/`feature_type_code`→`fire_trail`, `class_code=6`→`management_trail`, `class_code=7`→`4wd_track`, `class_code IN (8,9)` or `feature_type_code='trail'`→`walking_track` (`is_vehicular=FALSE`) |
| **DEECA Recreation Tracks** | `open-data-platform:recweb_tracks` | *(bbox only)* | `source_tracks_vic`: `access_dsc`/`trk_class`/`track_type` → `fire_trail`, `management_trail`, `4wd_track` (`4wd`/`vehicle`/`motor`/`drive`), else `walking_track` |
| **Vicmap Features of Interest** | `open-data-platform:foi_point` | `feature_subtype IN ('hut','camp ground','camping ground','picnic site','day visitor area','lookout','trailhead','survey monument')` | `source_pois`: mapped to `hut`, `campground`, `picnic`, `lookout`, `trig` |
| **DEECA Backcountry Huts** | `open-data-platform:recweb_hut` | *(bbox only)* | `source_pois`: `poi_type='hut'`, name=`name`/`hut_name`/`site_name` |
| **DEECA Recreation Sites** | `open-data-platform:recweb_site` | *(bbox only)* | `source_pois`: classified via `site_type`/`site_class`, default `campground` |
| **Vicmap Survey Marks** | `open-data-platform:geomark_point` | `feature_subtype IN ('survey monument','hut')` | `source_pois`: `poi_type='trig'`/`'hut'`, `elev_m` from `elev_m`/`elevation`/`height`/`ahd_height` |

*POI Deduplication Rule:* Drop duplicate point features sharing `(poi_type, lower(trim(name)), round(lon * 1000), round(lat * 1000))` (~100 m spatial bucket), preferring records with populated `elev_m` and state authoritative metadata. Placeholder names (`unnamed`, `unknown`, `none`, `null`, `n/a`) are normalized to `NULL`.

#### D. Geoscience Australia National Surface Hydrology (`CC BY 4.0`)
- **Base MapServer:** `https://services.ga.gov.au/gis/rest/services/Surface_Hydrology/MapServer`
  - **Layer `1`** (`/1/query`): Water points (`name,featuretype,type,perenniality`) — bores, waterholes, springs → `source_pois` (`poi_type='water_point'`)
  - **Layer `3`** (`/3/query`): Watercourse lines (`where="featuretype <> 'Connector'"`, `outFields="name,featuretype,perenniality,hierarchy"`) → `source_waterways` (primary creek/river crossing input)
  - **Layer `5`** (`/5/query`): Waterbody polygons (`name,featuretype,perenniality`) → `source_waterways` (for lake/ford context)

#### E. DCCEEW CAPAD 2025 Protected Areas (`CC BY 4.0`)
- **Endpoint:** `https://gis.environment.gov.au/gispubmap/rest/services/ogc_services/CAPAD/FeatureServer/0/query`
- **Params:** `outFields="NAME,TYPE,TYPE_ABBR,IUCN,STATE"`, `maxAllowableOffset="0.00005"` (~5 m vertex simplification keeps massive National Park / IPA polygons `< 500 KB`)
- **Target Table:** `source_capad_parks` → canonical `capad_parks` (used for Track Inspector tenure display and NSW NPWS park-alert joins)

#### F. Elevation (DEM) — GA 5 m LiDAR WCS + DEA SRTM 1s COG (`CC BY 4.0`, Datum `AHD`)
1. **Tier 1 — GA National 5 m LiDAR WCS 1.0.0** (used only for compact corridor/tactical tiles `<= 1.0° × 1.0°` where LiDAR coverage is complete):
   - **URL:** `https://services.ga.gov.au/gis/services/DEM_LiDAR_5m_2025/MapServer/WCSServer`
   - **Params:** `SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&COVERAGE=1&CRS=EPSG:4283&BBOX=<w>,<s_lat>,<e>,<n_lat>&RESX=0.000055&RESY=0.000052&FORMAT=GeoTIFF` (`0.000110 × 0.000104` for ~10 m regional tiles)
   - **No-Mix Invariant:** Never mosaic Tier 1 (5 m LiDAR) and Tier 2 (SRTM 1s) pixels within the same DEM tile/clip, to avoid artificial elevation steps along LiDAR survey boundaries.
   - **Coastal-Water vs. Missing-LiDAR Cliff Guard (Mandatory):**
     - Read a downsampled `min(cols, 256) × min(rows, 256)` `Float32` buffer from band 1, filtering non-finite values, `nodata`, and outliers outside `[-500, 5000]` m.
     - Compute `valid_frac`, `exact_zero_frac`, `min_elev`, `max_elev`, and `zero_cliff_ratio`: the fraction of 4-neighbor `(0.0 m → valid non-zero)` pixel edges where the non-zero neighbor exceeds `cliff_threshold_m = 15.0 m`.
     - Real coastal/estuary water (e.g., Sydney Harbour, Port Phillip) has `0.0 m` pixels adjacent to low shoreline (`zero_cliff_ratio` ≈ `2–6%`). Missing inland LiDAR masks from GA WCS fill missing zones with `0.0 m` directly abutting ridges (`zero_cliff_ratio` ≈ `95–100%`).
     - **Acceptance rule:** Accept Tier 1 5 m LiDAR **only if**:
       - `valid_frac >= 0.95` **AND** `max_elev > min_elev + 0.1` **AND**
       - (`exact_zero_frac <= 0.05` **OR** (`exact_zero_frac <= 0.45` **AND** `zero_cliff_ratio <= 0.15`)).
       - Otherwise discard the clip and fall back cleanly to Tier 2 DEA SRTM 1s.
2. **Tier 2 — DEA GA SRTM 1-second COG on AWS S3 (`ap-southeast-2`):**
   - **Smoothed DEM (default for track slope draping, hillshade, contours):**  
     `https://dea-public-data.s3.ap-southeast-2.amazonaws.com/projects/elevation/ga_srtm_dem1sv1_0/dems1sv1_0.tif`
   - **Hydro-Enforced DEM (hydrology validation):**  
     `https://dea-public-data.s3.ap-southeast-2.amazonaws.com/projects/elevation/ga_srtm_dem1sv1_0/demh1sv1_0.tif`
   - **Streamed via GDAL `/vsicurl/`:** `gdal_translate -of GTiff -ot Float32 -co COMPRESS=DEFLATE -co PREDICTOR=3 -co TILED=YES -co BIGTIFF=IF_SAFER -projwin <min_x> <max_y> <max_x> <min_y> -projwin_srs EPSG:4326 /vsicurl/<url> <out.tif>`
   - **Backup WCS (if S3 COG fails):**  
     `https://services.ga.gov.au/gis/services/DEM_SRTM_1Second_Hydro_Enforced_2024/MapServer/WCSServer` (`CRS=EPSG:4326`, `RESX=0.000277778`, `RESY=0.000277778`, `FORMAT=GeoTIFF`)

#### G. Live Closures, Incident Feeds & Rainfall Connectors (Slices 04 & 07)
- **VIC Seasonal Road Closures (MapShareVic / DEECA):** DEECA seasonal road closure WFS/ArcGIS feed (pluggable `ClosureFeedConnector`, configurable URL via env, synced every 6 h).
- **VicEmergency Incidents:** `https://emergency.vic.gov.au/public/events-geojson.json` (GeoJSON, synced every 15 min).
- **NSW RFS Major Incidents:** `https://www.rfs.nsw.gov.au/feeds/majorIncidents.json` (GeoJSON, synced every 15 min).
- **NSW NPWS Park Alerts:** Official NSW NPWS alerts JSON/API joined to `capad_parks` by park name/ID (synced every 60 min).
- **Open-Meteo 72 h Rainfall (`CC BY 4.0`):** `https://api.open-meteo.com/v1/forecast` (`latitude`, `longitude`, `past_days=3`, `forecast_days=1`, `hourly=precipitation`, `daily=precipitation_sum`, `timezone=UTC`) sampled on a ~15 km regional grid (`0.15°`) for active track centroids so API load stays minimal.

#### H. User GPX / GeoJSON / CSV Upload Connector (Adapted from `overlay_user.py`)
- Supported extensions: `.gpx`, `.geojson`, `.json`, `.csv`, `.gpkg`, `.kml` via GDAL/OGR (`ogr2ogr` / `osgeo.ogr`) plus pure-Python GPX XML timestamp/elevation/extension parsing for high-precision telemetry ingestion (Slice 06).
- GPX layer mapping: `waypoints → points`, `tracks → lines`, `routes → lines`.
- CSV auto-detection of lon/lat columns: `{lon, lng, long, longitude, x, easting}` and `{lat, latitude, y, northing}`.

### 1.3 Pagination, Disk Caching & `< 300 MB` Peak RAM Streaming Pack Pattern

1. **Deterministic Enclosing BBox Disk Cache:**
   - Round query bounding boxes outward to `0.001°` (`_round_enclosing_bbox`), compute cache key `sha256(f"{min_x:.3f},{min_y:.3f},{max_x:.3f},{max_y:.3f}")[:24]`, write atomically via `<file>.tmp` + `Path.replace()`.
2. **ArcGIS & WFS Pagination + Spatial Tiling:**
   - ArcGIS REST: `resultRecordCount=1000`, increment `resultOffset` while `exceededTransferLimit` is true (up to `max_pages=5` per tile; subdivide any tile that hits the cap). Retries: `2` attempts, connect timeout `8 s`, read timeout `25 s`. Outages log warnings and preserve last-known DB state.
   - OGC WFS 2.0.0: `count=2500`, partition statewide bounding boxes into `1.0° × 1.0°` tiles (`_iter_spatial_tiles`) so every response stays bounded in RAM.
3. **Memory-Bounded Disk-to-Disk Streaming (`< 300 MB` Peak RAM):**
   - Large downloads (Geofabrik PBF) stream in `1 MiB` (`1_048_576` B) chunks to `.part` files with atomic rename.
   - All GDAL/OGR subprocesses run with strict memory caps:
     ```ini
     GDAL_CACHEMAX=128
     OMP_THREAD_LIMIT=1
     GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR
     CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.vrt
     VSI_CACHE=TRUE
     VSI_CACHE_SIZE=67108864
     GDAL_HTTP_MAX_RETRY=3
     GDAL_HTTP_RETRY_DELAY=2
     ```
   - Vector tiles generated disk-to-disk via `tippecanoe --drop-densest-as-needed --extend-zooms-if-still-dropping -o <out.pmtiles>`; terrain RGB tiles generated via `gdalwarp` → `rio rgbify` → `pmtiles convert` (zero PyQGIS).

### 1.4 Required Attribution Strings (Locked — Never Strip)

All map views, Track Inspectors, offline pack manifests (`pack.json`), and GPX/GeoJSON exports **must** preserve the applicable attribution strings:

| Source | License | Exact Required Attribution String |
| :--- | :--- | :--- |
| **OpenStreetMap** | `ODbL` | `Map data © OpenStreetMap contributors (ODbL) — openstreetmap.org/copyright` |
| **Geoscience Australia / DEA DEM** | `CC BY 4.0` | `Elevation data: © Commonwealth of Australia (Geoscience Australia / Digital Earth Australia) (CC BY 4.0)` |
| **GA Surface Hydrology** | `CC BY 4.0` | `Surface Hydrology: © Commonwealth of Australia (Geoscience Australia) & AusHydro Contributors (CC BY 4.0)` |
| **DCCEEW CAPAD 2025** | `CC BY 4.0` | `Protected Areas (CAPAD 2025): © Commonwealth of Australia (DCCEEW) (CC BY 4.0)` |
| **NSW Spatial Services** | `CC BY 4.0` | `NSW Trails, Facilities & Trig Stations: © State of New South Wales (Spatial Services, DCS) (CC BY 4.0)` |
| **Victoria DEECA / DTP Vicmap** | `CC BY 4.0` | `Victoria Trails, Huts, Sites & Closures: © State of Victoria (DEECA / DTP Vicmap) (CC BY 4.0)` |
| **Open-Meteo** | `CC BY 4.0` | `Weather & Rainfall data by Open-Meteo.com (CC BY 4.0)` |
| **State Emergency & Park Feeds** | Public Agency | `Live incident & park closure alerts: VicEmergency, NSW RFS, and NSW NPWS (attributed per alert with official source URL)` |

---

## 2. Complete PostGIS Spatial Schema (Slices 01–09)

### 2.1 CRS, SRID & ODbL Separation Invariants

1. **Storage SRID (`EPSG:4326`):** All PostGIS `GEOMETRY` columns store 2D WGS84 coordinates (`SRID=4326`) for direct GeoJSON, MapLibre, and Tippecanoe compatibility and lean GIST index footprints. Elevation profiles are stored as structured JSONB (`[[dist_m, elev_m], ...]`) alongside numeric summary columns (`elev_min_m`, `elev_max_m`, `elev_gain_m`, `elev_loss_m`).
2. **Australian Metric Calculation SRID (`EPSG:3577` + Local UTM):**
   - **`EPSG:3577` (GDA94 / Australian Albers Equal Area)** is used for all PostGIS planar metric calculations: segment lengths, `[50, 100]` m chunking, 20 m conflation buffers, 12 m reconciliation corridors, Hausdorff distance, and 250 m privacy buffer trimming — eliminating UTM Zone 54S/55S/56S boundary seams across VIC & NSW.
   - **Local UTM (`EPSG:32754` / `32755` / `32756`)** is used for local DEM raster warping (`gdalwarp`), slope derivation (`gdaldem slope`), hillshade, contours, and MGRS formatting.
3. **Strict OSM ODbL Separation (Collective Database Architecture):**
   - **Tier A — Dedicated Source Tables:** `source_tracks_osm` (ODbL) is physically isolated from CC BY 4.0 state/national tables (`source_tracks_vic`, `source_tracks_nsw`, `source_waterways`, `source_pois`, `source_capad_parks`).
   - **Tier B — Canonical Conflated Network Tables:** `tracks`, `track_segments`, `segment_reconciliation_log`, `pois`, `capad_parks` track exact provenance (`primary_source`, `source_refs JSONB`, `has_osm_source BOOLEAN` / `source_license`). Any table/export containing OSM geometry retains OSM ODbL attribution.
   - **Tier C — Independent Community, Telemetry & Live Intelligence Tables:** `track_segment_grades`, `closures_alerts`, `segment_closure_links`, `users`, `vehicle_profiles`, `user_favorites`, `user_pins`, `trips`, `telemetry_observations`, `unmapped_track_candidates`, `condition_reports`, `condition_report_votes`, `rainfall_snapshots`, `feed_sync_status`, `offline_packs` contain **zero raw OSM geometries and zero raw OSM tag blobs**. They link to the canonical network solely via TrailForge's opaque deterministic `segment_id` (`TEXT`) or independent user/agency WGS84 coordinates.
4. **No Cascade Orphaning:** `track_segments` uses soft-retirement (`is_active = FALSE`, `retired_at`, `superseded_by_segment_id`) instead of hard `DELETE`. Dependent tables reference `track_segments(segment_id) ON DELETE RESTRICT`.

---

### 2.2 Tier A — Upstream Source Tables (6 Tables)

#### 1. `source_tracks_osm` *(ODbL — Slice 01/02)*
Dedicated OpenStreetMap way staging table.

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Surrogate row ID |
| `osm_way_id` | `BIGINT` | `NOT NULL UNIQUE` | Upstream OSM way ID |
| `osm_version` | `INTEGER` | `NULL` | OSM changeset version |
| `name` | `TEXT` | `NULL` | Cleaned `name` tag |
| `highway` | `TEXT` | `NOT NULL` | `track`, `path`, `unclassified`, `service`, `ford` |
| `surface` | `TEXT` | `NULL` | Normalized OSM `surface` |
| `tracktype` | `TEXT` | `NULL` | `grade1`–`grade5` |
| `smoothness` | `TEXT` | `NULL` | OSM `smoothness` |
| `four_wd_only` | `TEXT` | `NULL` | Raw `4wd_only` (`yes`, `recommended`, `no`) |
| `access` | `TEXT` | `NULL` | Effective access (`public`, `private`, `forestry`, `no`, etc.) |
| `motor_vehicle` | `TEXT` | `NULL` | OSM `motor_vehicle` tag |
| `bridge` | `TEXT` | `NULL` | OSM `bridge` tag |
| `ford` | `TEXT` | `NULL` | OSM `ford` tag |
| `has_gate_barrier` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | True if way or incident node has `barrier=gate`/`lift_gate`/`chain` |
| `tags` | `JSONB` | `NOT NULL DEFAULT '{}'::jsonb` | Full raw OSM tags (isolated in Tier A) |
| `geom` | `GEOMETRY(LineString, 4326)` | `NOT NULL` | Original OSM way geometry |
| `source_snapshot_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | PBF/extract timestamp |
| `ingested_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Import time |

- **Indexes:** `GIST (geom)`, `BTREE (highway)`, `BTREE (osm_way_id)`.

#### 2. `source_tracks_vic` *(CC BY 4.0 — Slice 01/02)*
Dedicated Victoria DataVic/Vicmap (`tr_road` & `recweb_tracks`) source table.

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Surrogate row ID |
| `source_layer` | `TEXT` | `NOT NULL CHECK (source_layer IN ('tr_road','recweb_tracks'))` | Upstream WFS layer |
| `source_feature_id` | `TEXT` | `NOT NULL` | `pfi` / `ufi` / GeoJSON feature ID |
| `name` | `TEXT` | `NULL` | Cleaned `ezi_road_name_label` / `name` |
| `trail_class` | `TEXT` | `NOT NULL` | `4wd_track`, `fire_trail`, `management_trail`, `walking_track` |
| `class_code` | `INTEGER` | `NULL` | Vicmap `class_code` (`6`, `7`, `8`, `9`) |
| `surface` | `TEXT` | `NULL` | Mapped `road_seal` (`sealed`, `unsealed`, `natural`) |
| `is_vehicular` | `BOOLEAN` | `NOT NULL DEFAULT TRUE` | False for `walking_track` |
| `raw_props` | `JSONB` | `NOT NULL DEFAULT '{}'::jsonb` | Raw WFS properties |
| `geom` | `GEOMETRY(MultiLineString, 4326)` | `NOT NULL` | Projected WGS84 geometry |
| `ingested_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Import time |

- **Constraints:** `UNIQUE (source_layer, source_feature_id)`
- **Indexes:** `GIST (geom)`, `BTREE (trail_class)`, `BTREE (source_layer)`.

#### 3. `source_tracks_nsw` *(CC BY 4.0 — Slice 01/02)*
Dedicated NSW Spatial Services (`NSW_Transport_Theme` layers 5 & 9) source table.

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Surrogate row ID |
| `source_layer` | `TEXT` | `NOT NULL CHECK (source_layer IN ('nsw_fire_trails_l9','nsw_tracks_l5'))` | ArcGIS MapServer layer |
| `source_feature_id` | `TEXT` | `NOT NULL` | `OBJECTID` / `brimsid` |
| `name` | `TEXT` | `NULL` | Composed NSW road/fire trail name |
| `trail_class` | `TEXT` | `NOT NULL` | `fire_trail`, `4wd_track`, `management_trail`, `walking_track` |
| `class_subtype` | `INTEGER` | `NULL` | NSW `classsubtype` (`5` or `6`) |
| `surface` | `TEXT` | `NULL` | Mapped `surface` (`sealed`, `unsealed`, `natural`) |
| `operational_status` | `TEXT` | `NULL` | NSW `operationalstatus` |
| `is_vehicular` | `BOOLEAN` | `NOT NULL DEFAULT TRUE` | False for `walking_track` |
| `raw_props` | `JSONB` | `NOT NULL DEFAULT '{}'::jsonb` | Raw ArcGIS attributes |
| `geom` | `GEOMETRY(MultiLineString, 4326)` | `NOT NULL` | WGS84 geometry |
| `ingested_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Import time |

- **Constraints:** `UNIQUE (source_layer, source_feature_id)`
- **Indexes:** `GIST (geom)`, `BTREE (trail_class)`, `BTREE (source_layer)`.

#### 4. `source_waterways` *(CC BY 4.0 GA / ODbL OSM tagged — Slice 01/02)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Surrogate row ID |
| `source_provider` | `TEXT` | `NOT NULL CHECK (source_provider IN ('ga_hydro','osm'))` | Provider key |
| `source_license` | `TEXT` | `NOT NULL CHECK (source_license IN ('CC-BY-4.0','ODbL'))` | Explicit license tag |
| `source_feature_id` | `TEXT` | `NOT NULL` | Upstream ID |
| `name` | `TEXT` | `NULL` | River/creek name |
| `feature_type` | `TEXT` | `NOT NULL` | `Watercourse`, `River`, `Stream`, `Lake` (excludes `Connector`) |
| `perenniality` | `TEXT` | `NOT NULL DEFAULT 'Unknown'` | `Perennial`, `Intermittent`, `Ephemeral`, `Unknown` |
| `hierarchy` | `TEXT` | `NULL` | `Major`, `Minor` |
| `geom` | `GEOMETRY(Geometry, 4326)` | `NOT NULL` | `LineString`/`MultiLineString`/`MultiPolygon` |
| `ingested_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Import time |

- **Constraints:** `UNIQUE (source_provider, source_feature_id)`
- **Indexes:** `GIST (geom)`, `BTREE (perenniality)`, `BTREE (source_provider)`.

#### 5. `source_pois` *(CC BY 4.0 VIC/NSW/GA / ODbL OSM tagged — Slice 01/02)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Surrogate row ID |
| `source_provider` | `TEXT` | `NOT NULL CHECK (source_provider IN ('vic_foi','vic_recweb_hut','vic_recweb_site','vic_geomark','nsw_poi','nsw_trig','nsw_hut','ga_hydro_pt','osm'))` | Upstream source |
| `source_license` | `TEXT` | `NOT NULL CHECK (source_license IN ('CC-BY-4.0','ODbL'))` | Explicit license tag |
| `source_feature_id` | `TEXT` | `NOT NULL` | Upstream ID |
| `name` | `TEXT` | `NULL` | POI name |
| `poi_type` | `TEXT` | `NOT NULL CHECK (poi_type IN ('campground','hut','trig','lookout','picnic','water_point','gate','trailhead','fuel'))` | Normalized POI class |
| `state` | `TEXT` | `NULL CHECK (state IN ('VIC','NSW','ACT'))` | State/territory |
| `elev_m` | `DOUBLE PRECISION` | `NULL` | Elevation in AHD meters |
| `raw_props` | `JSONB` | `NOT NULL DEFAULT '{}'::jsonb` | Raw properties |
| `geom` | `GEOMETRY(Point, 4326)` | `NOT NULL` | WGS84 point |
| `ingested_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Import time |

- **Constraints:** `UNIQUE (source_provider, source_feature_id)`
- **Indexes:** `GIST (geom)`, `BTREE (poi_type)`, `BTREE (state)`.

#### 6. `source_capad_parks` *(CC BY 4.0 — Slice 01/02)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Surrogate row ID |
| `source_feature_id` | `TEXT` | `NOT NULL UNIQUE` | CAPAD `PA_ID` / `OBJECTID` |
| `name` | `TEXT` | `NOT NULL` | Protected area `NAME` |
| `park_type` | `TEXT` | `NOT NULL` | `TYPE` (e.g. `National Park`, `State Forest`, `Nature Reserve`) |
| `type_abbr` | `TEXT` | `NULL` | `TYPE_ABBR` (`NP`, `NR`, `IPA`) |
| `iucn` | `TEXT` | `NULL` | IUCN category (`Ia`, `II`, etc.) |
| `state` | `TEXT` | `NOT NULL` | `VIC`, `NSW`, `ACT` |
| `geom` | `GEOMETRY(MultiPolygon, 4326)` | `NOT NULL` | Simplified WGS84 polygon (`maxAllowableOffset=0.00005`) |
| `ingested_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Import time |

- **Indexes:** `GIST (geom)`, `BTREE (state, name)`.

---

### 2.3 Tier B — Canonical Conflated Network & Reference Tables (5 Tables)

#### 7. `tracks` *(Slice 01/02/03)*
Logical named trails grouping contiguous canonical `track_segments`.

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `track_id` | `TEXT` | `PRIMARY KEY` | Deterministic slug+hash ID (`tftrk_<12hex>`) |
| `name` | `TEXT` | `NOT NULL` | Display name (e.g. `Davies Plain Track`) |
| `alt_names` | `TEXT[]` | `NOT NULL DEFAULT '{}'` | Alternate/state aliases for search |
| `state` | `TEXT` | `NOT NULL CHECK (state IN ('VIC','NSW','ACT','VIC/NSW'))` | Jurisdiction(s) traversed |
| `primary_source` | `TEXT` | `NOT NULL CHECK (primary_source IN ('osm','vic','nsw'))` | Dominant source |
| `has_osm_source` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | ODbL attribution flag |
| `trail_class` | `TEXT` | `NOT NULL CHECK (trail_class IN ('4wd_track','fire_trail','management_trail','unsealed_road','walking_track'))` | Dominant class |
| `is_vehicular` | `BOOLEAN` | `NOT NULL DEFAULT TRUE` | False if foot-only |
| `total_length_m` | `DOUBLE PRECISION` | `NOT NULL DEFAULT 0` | Sum of active segment lengths (`EPSG:3577`) |
| `elev_min_m` | `DOUBLE PRECISION` | `NULL` | Minimum elevation (AHD m) |
| `elev_max_m` | `DOUBLE PRECISION` | `NULL` | Maximum elevation (AHD m) |
| `elev_gain_m` | `DOUBLE PRECISION` | `NOT NULL DEFAULT 0` | Cumulative elevation gain (m) |
| `max_slope_deg` | `DOUBLE PRECISION` | `NULL` | Peak segment max slope |
| `waterway_crossing_count` | `INTEGER` | `NOT NULL DEFAULT 0` | Total unbridged crossings |
| `primary_park_id` | `BIGINT` | `NULL REFERENCES capad_parks(id) ON DELETE SET NULL` | Dominant CAPAD tenure |
| `bbox_geom` | `GEOMETRY(Polygon, 4326)` | `NULL` | Cached bounding envelope |
| `is_active` | `BOOLEAN` | `NOT NULL DEFAULT TRUE` | Soft-retirement flag |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | First created |
| `updated_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Last refreshed |

- **Indexes:** `GIST (bbox_geom)`, `BTREE (state, is_active)`, `BTREE (lower(name))`, `GIN (alt_names)`.

#### 8. `track_segments` *(Slice 01/02 — Core Canonical 50–100 m Unit)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `segment_id` | `TEXT` | `PRIMARY KEY` | Deterministic stable ID (`tfseg_<16hex>`) |
| `track_id` | `TEXT` | `NULL REFERENCES tracks(track_id) ON DELETE SET NULL` | Parent logical track |
| `segment_seq` | `INTEGER` | `NULL` | Order along parent track |
| `geom_fingerprint` | `CHAR(64)` | `NOT NULL` | SHA-256 of canonicalized 5-anchor `1e-5°` geometry |
| `geom` | `GEOMETRY(LineString, 4326)` | `NOT NULL` | Direction-normalized 2D WGS84 geometry |
| `start_node_key` | `TEXT` | `NOT NULL` | Quantized `1e-5°` start junction key (for HMM topology) |
| `end_node_key` | `TEXT` | `NOT NULL` | Quantized `1e-5°` end junction key (for HMM topology) |
| `length_m` | `DOUBLE PRECISION` | `NOT NULL CHECK (length_m > 0)` | Metric length in `EPSG:3577` (target `50–100 m`) |
| `bearing_deg` | `DOUBLE PRECISION` | `NOT NULL CHECK (bearing_deg >= 0 AND bearing_deg < 360)` | Forward start→end bearing |
| `state` | `TEXT` | `NOT NULL CHECK (state IN ('VIC','NSW','ACT'))` | State at midpoint |
| `name` | `TEXT` | `NULL` | Segment display name |
| `trail_class` | `TEXT` | `NOT NULL CHECK (trail_class IN ('4wd_track','fire_trail','management_trail','unsealed_road','walking_track'))` | Canonical track class |
| `is_vehicular` | `BOOLEAN` | `NOT NULL DEFAULT TRUE` | Eligible for 4WD HMM matching & grading |
| `surface_class` | `TEXT` | `NOT NULL DEFAULT 'unknown' CHECK (surface_class IN ('sealed','gravel_compacted','dirt_unsealed','rough_rock_mud_sand','unknown'))` | Normalized surface |
| `tracktype` | `TEXT` | `NULL CHECK (tracktype IN ('grade1','grade2','grade3','grade4','grade5'))` | Normalized tracktype |
| `is_4wd_only` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | `4wd_only=yes` |
| `is_4wd_recommended` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | `4wd_only=recommended` |
| `access_status` | `TEXT` | `NOT NULL DEFAULT 'public' CHECK (access_status IN ('public','seasonal','permit','private','restricted','closed'))` | Static access tag summary |
| `has_gate_barrier` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | OSM/state gate present on segment |
| `is_bridged` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | Segment is bridged across waterway |
| `elev_min_m` | `DOUBLE PRECISION` | `NULL` | Min AHD elevation (m) |
| `elev_max_m` | `DOUBLE PRECISION` | `NULL` | Max AHD elevation (m) |
| `elev_start_m` | `DOUBLE PRECISION` | `NULL` | Elevation at canonical start (m) |
| `elev_end_m` | `DOUBLE PRECISION` | `NULL` | Elevation at canonical end (m) |
| `elev_gain_m` | `DOUBLE PRECISION` | `NOT NULL DEFAULT 0` | Positive climb along canonical forward direction |
| `elev_loss_m` | `DOUBLE PRECISION` | `NOT NULL DEFAULT 0` | Descent along canonical forward direction |
| `max_slope_deg` | `DOUBLE PRECISION` | `NULL` | Max slope over `>= 20 m` rolling window (degrees) |
| `sustained_slope_deg` | `DOUBLE PRECISION` | `NULL` | Sustained slope (degrees) |
| `is_steep_highlight` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | True when `max_slope_deg >= 14.0` |
| `is_above_snowline` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | True when `elev_max_m >= 1000.0` |
| `elevation_profile_m` | `JSONB` | `NOT NULL DEFAULT '[]'::jsonb` | `[[dist_m, elev_m], ...]` sampled every ~10 m |
| `dem_tier` | `TEXT` | `NULL CHECK (dem_tier IN ('5m-lidar','srtm-1s','synthetic'))` | Provenance of elevation draping |
| `waterway_crossing_count` | `INTEGER` | `NOT NULL DEFAULT 0` | Unbridged waterway intersections |
| `has_perennial_crossing` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | Perennial river/creek or `ford=yes` |
| `capad_park_id` | `BIGINT` | `NULL REFERENCES capad_parks(id) ON DELETE SET NULL` | Park/reserve tenure at midpoint |
| `primary_source` | `TEXT` | `NOT NULL CHECK (primary_source IN ('osm','vic','nsw'))` | Winning conflation source |
| `has_osm_source` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | ODbL attribution flag |
| `source_refs` | `JSONB` | `NOT NULL DEFAULT '[]'::jsonb` | `[{"source":"osm","source_id":"way/1002"}, ...]` |
| `is_active` | `BOOLEAN` | `NOT NULL DEFAULT TRUE` | Soft-retirement flag |
| `retired_at` | `TIMESTAMPTZ` | `NULL` | Timestamp when retired/superseded |
| `superseded_by_segment_id` | `TEXT` | `NULL REFERENCES track_segments(segment_id) ON DELETE SET NULL` | Lineage pointer on split/merge |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | First minted |
| `updated_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Last updated |

- **Indexes:**
  - `GIST (geom)` (partial `WHERE is_active = TRUE` plus full GIST on `geom`)
  - `BTREE (geom_fingerprint)`
  - `BTREE (track_id, segment_seq)`
  - `BTREE (start_node_key)`, `BTREE (end_node_key)` *(critical for Slice 06 in-PostGIS HMM adjacency transitions)*
  - `BTREE (capad_park_id)`
  - `BTREE (is_active, is_vehicular, state)`

#### 9. `segment_reconciliation_log` *(Slice 02 — Re-Import Lineage Audit)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Log entry ID |
| `import_run_id` | `UUID` | `NOT NULL` | Batch ingestion run identifier |
| `reconcile_action` | `TEXT` | `NOT NULL CHECK (reconcile_action IN ('created','exact_match','spatial_preserved','split','merge','retired'))` | Reconciliation outcome |
| `old_segment_id` | `TEXT` | `NULL REFERENCES track_segments(segment_id) ON DELETE RESTRICT` | Previous ID |
| `new_segment_id` | `TEXT` | `NULL REFERENCES track_segments(segment_id) ON DELETE RESTRICT` | Resulting ID |
| `overlap_ratio` | `DOUBLE PRECISION` | `NULL` | Corridor overlap `[0, 1]` |
| `hausdorff_m` | `DOUBLE PRECISION` | `NULL` | Hausdorff distance in `EPSG:3577` |
| `heading_diff_deg` | `DOUBLE PRECISION` | `NULL` | Acute bearing delta |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Audit timestamp |

- **Indexes:** `BTREE (import_run_id)`, `BTREE (old_segment_id)`, `BTREE (new_segment_id)`.

#### 10. `pois` *(Slice 01/02/03)*
Deduplicated canonical backcountry POIs (camps, huts, trigs, lookouts, gates, water points).

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Canonical POI ID |
| `poi_key` | `TEXT` | `NOT NULL UNIQUE` | Deterministic dedupe key (`poi_type:slug:lon3:lat3`) |
| `name` | `TEXT` | `NOT NULL` | Display name |
| `poi_type` | `TEXT` | `NOT NULL CHECK (poi_type IN ('campground','hut','trig','lookout','picnic','water_point','gate','trailhead','fuel'))` | Category |
| `state` | `TEXT` | `NOT NULL CHECK (state IN ('VIC','NSW','ACT'))` | Jurisdiction |
| `elev_m` | `DOUBLE PRECISION` | `NULL` | Elevation (AHD m) |
| `access_note` | `TEXT` | `NULL` | Gate/access notes |
| `nearest_segment_id` | `TEXT` | `NULL REFERENCES track_segments(segment_id) ON DELETE SET NULL` | Nearest segment within 250 m |
| `capad_park_id` | `BIGINT` | `NULL REFERENCES capad_parks(id) ON DELETE SET NULL` | Containing park |
| `primary_source` | `TEXT` | `NOT NULL` | Winning provider |
| `source_license` | `TEXT` | `NOT NULL CHECK (source_license IN ('CC-BY-4.0','ODbL'))` | License tag |
| `source_refs` | `JSONB` | `NOT NULL DEFAULT '[]'::jsonb` | Merged source references |
| `geom` | `GEOMETRY(Point, 4326)` | `NOT NULL` | WGS84 point |
| `updated_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Last refreshed |

- **Indexes:** `GIST (geom)`, `BTREE (poi_type, state)`, `BTREE (lower(name))`, `BTREE (nearest_segment_id)`.

#### 11. `capad_parks` *(Slice 01/02/03/04)*
Canonical protected area polygons for tenure attribution and park-level alert linking.

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Canonical park ID |
| `capad_external_id` | `TEXT` | `NOT NULL UNIQUE` | Stable CAPAD ID |
| `name` | `TEXT` | `NOT NULL` | Official park name |
| `park_type` | `TEXT` | `NOT NULL` | e.g. `National Park`, `Nature Reserve` |
| `type_abbr` | `TEXT` | `NULL` | `NP`, `NR`, `IPA` |
| `iucn` | `TEXT` | `NULL` | IUCN category |
| `state` | `TEXT` | `NOT NULL CHECK (state IN ('VIC','NSW','ACT'))` | State/territory |
| `area_ha` | `DOUBLE PRECISION` | `NULL` | Area in hectares (`EPSG:3577`) |
| `geom` | `GEOMETRY(MultiPolygon, 4326)` | `NOT NULL` | Park boundary |
| `updated_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Last updated |

- **Indexes:** `GIST (geom)`, `BTREE (state, lower(name))`.

---

### 2.4 Tier C — Independent TrailForge Intelligence, Community & Live Tables (15 Tables)

> **ODbL Isolation Guarantee:** Every Tier C table below stores **zero raw OSM geometries and zero OSM tag blobs**. Links to the road/track network use solely TrailForge's opaque `segment_id`.

#### 12. `track_segment_grades` *(Slice 02 baseline + Slice 07 live/rainfall)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `segment_id` | `TEXT` | `PRIMARY KEY REFERENCES track_segments(segment_id) ON DELETE RESTRICT` | 1:1 with canonical segment |
| `baseline_grade` | `TEXT` | `NOT NULL CHECK (baseline_grade IN ('Easy','Medium','Hard','Extreme'))` | Deterministic Day-1 grade |
| `baseline_score` | `DOUBLE PRECISION` | `NOT NULL CHECK (baseline_score >= 0 AND baseline_score <= 100)` | Deterministic `0–100` score |
| `baseline_breakdown` | `JSONB` | `NOT NULL` | Explainable static terrain/tag breakdown (Section 3.4) |
| `live_grade` | `TEXT` | `NULL CHECK (live_grade IN ('Easy','Medium','Hard','Extreme'))` | Crowdsourced telemetry-adjusted grade (Slice 07) |
| `live_score` | `DOUBLE PRECISION` | `NULL CHECK (live_score >= 0 AND live_score <= 100)` | Blended live score |
| `live_breakdown` | `JSONB` | `NULL` | Telemetry evidence: sample count, freshness, vehicle-class split, avg speed, hotspots |
| `effective_grade` | `TEXT` | `NOT NULL CHECK (effective_grade IN ('Easy','Medium','Hard','Extreme'))` | Displayed grade (`live_grade` when confidence sufficient, else `baseline_grade`) |
| `confidence_level` | `TEXT` | `NOT NULL DEFAULT 'baseline_only' CHECK (confidence_level IN ('baseline_only','low','medium','high'))` | Evidence confidence tier |
| `confidence_score` | `DOUBLE PRECISION` | `NOT NULL DEFAULT 0.0 CHECK (confidence_score >= 0 AND confidence_score <= 1)` | Continuous `[0, 1]` confidence |
| `sample_count` | `INTEGER` | `NOT NULL DEFAULT 0` | Distinct anonymized traversal count |
| `last_driven_at` | `TIMESTAMPTZ` | `NULL` | Most recent traversal timestamp (displayed at date granularity publicly) |
| `avg_speed_kmh` | `DOUBLE PRECISION` | `NULL` | Recency-weighted mean speed |
| `avg_duration_s` | `DOUBLE PRECISION` | `NULL` | Recency-weighted traversal duration |
| `vehicle_class_split` | `JSONB` | `NOT NULL DEFAULT '{}'::jsonb` | e.g. `{"AWD":0,"Stock 4WD":4,"Touring 4WD":9,"Modified 4WD":2}` |
| `rainfall_context` | `JSONB` | `NULL` | Latest 72 h Open-Meteo precipitation & wet-condition risk flag |
| `wet_risk_level` | `TEXT` | `NOT NULL DEFAULT 'dry' CHECK (wet_risk_level IN ('dry','damp','wet','saturated'))` | Wet-weather risk state |
| `baseline_computed_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Last baseline calculation |
| `live_computed_at` | `TIMESTAMPTZ` | `NULL` | Last telemetry/rainfall recalculation |

- **Indexes:** `BTREE (effective_grade)`, `BTREE (confidence_level)`, `BTREE (last_driven_at DESC)`, `BTREE (wet_risk_level)`.

#### 13. `closures_alerts` *(Slice 04)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Alert ID |
| `source_feed` | `TEXT` | `NOT NULL CHECK (source_feed IN ('vic_mapshare_seasonal','vic_emergency','nsw_rfs','nsw_npws','osm_access'))` | Feed provider |
| `external_id` | `TEXT` | `NOT NULL` | Upstream incident/closure ID |
| `alert_type` | `TEXT` | `NOT NULL CHECK (alert_type IN ('seasonal_closure','road_closure','park_closure','bushfire','flood','hazard_warning','private_gate'))` | Normalized category |
| `severity` | `TEXT` | `NOT NULL CHECK (severity IN ('info','advisory','watch_and_act','closed','emergency'))` | Severity tier |
| `status` | `TEXT` | `NOT NULL DEFAULT 'active' CHECK (status IN ('active','scheduled','cleared','stale'))` | Lifecycle status |
| `title` | `TEXT` | `NOT NULL` | Short official headline |
| `description` | `TEXT` | `NULL` | Official agency details |
| `state` | `TEXT` | `NOT NULL CHECK (state IN ('VIC','NSW','ACT'))` | Jurisdiction |
| `source_url` | `TEXT` | `NULL` | Official link for verification |
| `capad_park_id` | `BIGINT` | `NULL REFERENCES capad_parks(id) ON DELETE SET NULL` | Linked park (for NPWS/Parks Vic alerts) |
| `geom` | `GEOMETRY(Geometry, 4326)` | `NULL` | Incident point, line, or polygon |
| `effective_from` | `TIMESTAMPTZ` | `NULL` | Closure/event start |
| `effective_to` | `TIMESTAMPTZ` | `NULL` | Scheduled reopening/expiry |
| `source_updated_at` | `TIMESTAMPTZ` | `NULL` | Upstream agency timestamp |
| `last_seen_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Last successful feed sync seeing this alert |
| `raw_payload` | `JSONB` | `NOT NULL DEFAULT '{}'::jsonb` | Raw agency alert JSON |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | First ingested |

- **Constraints:** `UNIQUE (source_feed, external_id)`
- **Indexes:** `GIST (geom)`, `BTREE (status, state, alert_type)`, `BTREE (capad_park_id)`, `BTREE (source_feed, last_seen_at DESC)`.

#### 14. `segment_closure_links` *(Slice 04)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `segment_id` | `TEXT` | `NOT NULL REFERENCES track_segments(segment_id) ON DELETE RESTRICT` | Affected canonical segment |
| `closure_id` | `BIGINT` | `NOT NULL REFERENCES closures_alerts(id) ON DELETE CASCADE` | Linked closure/alert |
| `link_method` | `TEXT` | `NOT NULL CHECK (link_method IN ('spatial_intersect','spatial_buffer','capad_park_join','osm_access_flag'))` | Provenance of link |
| `distance_m` | `DOUBLE PRECISION` | `NOT NULL DEFAULT 0` | Spatial separation (`0` if intersecting) |
| `linked_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Link timestamp |

- **Constraints:** `PRIMARY KEY (segment_id, closure_id)`
- **Indexes:** `BTREE (closure_id)`, `BTREE (segment_id)`.

#### 15. `feed_sync_status` *(Slice 04 & 07)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `feed_name` | `TEXT` | `PRIMARY KEY` | e.g. `vic_mapshare_seasonal`, `vic_emergency`, `nsw_rfs`, `nsw_npws`, `open_meteo_rainfall` |
| `source_url` | `TEXT` | `NOT NULL` | Configured upstream URL |
| `sync_interval_s` | `INTEGER` | `NOT NULL` | Expected polling cadence |
| `stale_after_s` | `INTEGER` | `NOT NULL` | Staleness threshold surfaced in UI |
| `last_attempt_at` | `TIMESTAMPTZ` | `NULL` | Most recent run attempt |
| `last_success_at` | `TIMESTAMPTZ` | `NULL` | Most recent healthy sync |
| `last_status` | `TEXT` | `NOT NULL DEFAULT 'never_run' CHECK (last_status IN ('never_run','ok','degraded','failed'))` | Health state |
| `consecutive_failures` | `INTEGER` | `NOT NULL DEFAULT 0` | Failure counter |
| `records_synced` | `INTEGER` | `NOT NULL DEFAULT 0` | Records processed in last success |
| `last_error` | `TEXT` | `NULL` | Last error message |

#### 16. `users` *(Slice 05)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `UUID` | `PRIMARY KEY DEFAULT gen_random_uuid()` | User ID |
| `email` | `TEXT` | `NOT NULL UNIQUE` | Normalized lowercase email |
| `display_name` | `TEXT` | `NOT NULL` | Public display handle for condition reports |
| `password_hash` | `TEXT` | `NOT NULL` | Argon2id / bcrypt password hash |
| `role` | `TEXT` | `NOT NULL DEFAULT 'user' CHECK (role IN ('user','moderator','admin'))` | Authorization role |
| `is_active` | `BOOLEAN` | `NOT NULL DEFAULT TRUE` | Account active flag |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Created timestamp |
| `updated_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Updated timestamp |

- **Indexes:** `BTREE (lower(email))`.

#### 17. `vehicle_profiles` *(Slice 05)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `UUID` | `PRIMARY KEY DEFAULT gen_random_uuid()` | Profile ID |
| `user_id` | `UUID` | `NOT NULL REFERENCES users(id) ON DELETE CASCADE` | Owner |
| `name` | `TEXT` | `NOT NULL` | e.g. `2021 LandCruiser 79 Touring` |
| `drivetrain` | `TEXT` | `NOT NULL CHECK (drivetrain IN ('awd','4wd_part_time','4wd_full_time'))` | Drivetrain (low-range inferred for `4wd_*` or explicit boolean) |
| `has_low_range` | `BOOLEAN` | `NOT NULL DEFAULT TRUE` | Dual-range transfer case |
| `lift_mm` | `INTEGER` | `NOT NULL DEFAULT 0 CHECK (lift_mm >= 0 AND lift_mm <= 300)` | Suspension lift (mm) |
| `ground_clearance_mm` | `INTEGER` | `NOT NULL DEFAULT 210 CHECK (ground_clearance_mm >= 120 AND ground_clearance_mm <= 500)` | Minimum diff/underbody clearance |
| `tire_class` | `TEXT` | `NOT NULL CHECK (tire_class IN ('HT','AT','MT'))` | Highway / All-Terrain / Mud-Terrain |
| `front_locker` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | Front diff lock |
| `rear_locker` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | Rear diff lock |
| `has_winch` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | Recovery winch |
| `has_snorkel` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | Raised air intake |
| `capability_class` | `TEXT` | `NOT NULL CHECK (capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD'))` | Deterministic normalized class |
| `is_default` | `BOOLEAN` | `NOT NULL DEFAULT FALSE` | Active default profile for user |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Created |
| `updated_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Updated |

- **Indexes:** `BTREE (user_id)`, partial unique index `UNIQUE (user_id) WHERE is_default = TRUE`.

#### 18. `user_favorites` *(Slice 05)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Favorite ID |
| `user_id` | `UUID` | `NOT NULL REFERENCES users(id) ON DELETE CASCADE` | Owner |
| `track_id` | `TEXT` | `NULL REFERENCES tracks(track_id) ON DELETE RESTRICT` | Favorited track |
| `segment_id` | `TEXT` | `NULL REFERENCES track_segments(segment_id) ON DELETE RESTRICT` | Optional favorited segment |
| `notes` | `TEXT` | `NULL` | Personal notes |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Saved timestamp |

- **Constraints:** `CHECK (track_id IS NOT NULL OR segment_id IS NOT NULL)`, `UNIQUE (user_id, track_id, segment_id)` (using partial unique indexes when either column is NULL).
- **Indexes:** `BTREE (user_id, created_at DESC)`.

#### 19. `user_pins` *(Slice 05 & 09)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `UUID` | `PRIMARY KEY DEFAULT gen_random_uuid()` | Pin ID |
| `user_id` | `UUID` | `NOT NULL REFERENCES users(id) ON DELETE CASCADE` | Owner |
| `client_uuid` | `UUID` | `NULL` | Idempotent PWA offline sync key |
| `title` | `TEXT` | `NOT NULL` | Pin label |
| `category` | `TEXT` | `NOT NULL DEFAULT 'waypoint' CHECK (category IN ('waypoint','camp','hazard','photo','water','recovery'))` | Pin type |
| `notes` | `TEXT` | `NULL` | Private notes |
| `elev_m` | `DOUBLE PRECISION` | `NULL` | Optional elevation |
| `geom` | `GEOMETRY(Point, 4326)` | `NOT NULL` | WGS84 pin location |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Created |
| `updated_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Updated |

- **Constraints:** `UNIQUE (user_id, client_uuid)`
- **Indexes:** `GIST (geom)`, `BTREE (user_id, created_at DESC)`.

#### 20. `trips` *(Slice 06 & 09 — User's Private Trip Logbook)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `UUID` | `PRIMARY KEY DEFAULT gen_random_uuid()` | Trip ID |
| `user_id` | `UUID` | `NOT NULL REFERENCES users(id) ON DELETE CASCADE` | Owner (private to user) |
| `client_uuid` | `UUID` | `NULL` | Idempotent PWA offline sync key |
| `vehicle_profile_id` | `UUID` | `NULL REFERENCES vehicle_profiles(id) ON DELETE SET NULL` | Rig driven |
| `vehicle_capability_class` | `TEXT` | `NOT NULL CHECK (vehicle_capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD'))` | Snapshot class at trip time |
| `title` | `TEXT` | `NULL` | User trip title |
| `visibility` | `TEXT` | `NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','community_anonymous'))` | Private logbook vs. anonymous contribution |
| `privacy_trim_m` | `DOUBLE PRECISION` | `NOT NULL DEFAULT 250.0 CHECK (privacy_trim_m >= 250.0 AND privacy_trim_m <= 5000.0)` | Enforced start/end privacy buffer |
| `contribution_nonce` | `UUID` | `NOT NULL DEFAULT gen_random_uuid()` | Rotated on visibility toggle to compute/revoke `contribution_hash` |
| `raw_geom` | `GEOMETRY(LineString, 4326)` | `NOT NULL` | Strictly private full GPS trace (never exposed publicly) |
| `trimmed_geom` | `GEOMETRY(LineString, 4326)` | `NULL` | Privacy-trimmed trace (`NULL` if trip `<= 2 * privacy_trim_m`) |
| `raw_distance_m` | `DOUBLE PRECISION` | `NOT NULL DEFAULT 0` | Full distance (`EPSG:3577`) |
| `trimmed_distance_m` | `DOUBLE PRECISION` | `NOT NULL DEFAULT 0` | Distance after privacy trim |
| `duration_s` | `DOUBLE PRECISION` | `NULL` | Total trip duration |
| `started_at` | `TIMESTAMPTZ` | `NULL` | Trip start time |
| `ended_at` | `TIMESTAMPTZ` | `NULL` | Trip end time |
| `match_status` | `TEXT` | `NOT NULL DEFAULT 'pending' CHECK (match_status IN ('pending','matched','partial','unmapped','skipped_private','skipped_too_short','failed'))` | Map-matching pipeline status |
| `matcher_name` | `TEXT` | `NULL` | e.g. `postgis_hmm_v1` |
| `avg_match_confidence` | `DOUBLE PRECISION` | `NULL` | Mean confidence `[0, 1]` |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Upload timestamp |

- **Constraints:** `UNIQUE (user_id, client_uuid)`
- **Indexes:** `GIST (raw_geom)`, `GIST (trimmed_geom)`, `BTREE (user_id, created_at DESC)`, `BTREE (match_status)`.

#### 21. `telemetry_observations` *(Slice 06 & 07 — STRICTLY ANONYMIZED, ZERO `user_id`)*
Publicly aggregatable segment traversals extracted strictly from privacy-trimmed community traces. **Contains NO `user_id` column and NO `trip_id` foreign key.**

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Observation ID |
| `segment_id` | `TEXT` | `NOT NULL REFERENCES track_segments(segment_id) ON DELETE RESTRICT` | Canonical segment traversed |
| `contribution_hash` | `CHAR(64)` | `NOT NULL` | `HMAC_SHA256(server_secret, trip.contribution_nonce)` — internal revocation key only, never exposed on APIs |
| `vehicle_capability_class` | `TEXT` | `NOT NULL CHECK (vehicle_capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD'))` | Normalized rig class |
| `direction` | `TEXT` | `NOT NULL CHECK (direction IN ('forward','reverse'))` | Relative to canonical segment orientation |
| `observed_at` | `TIMESTAMPTZ` | `NOT NULL` | Traversal timestamp (coarsened to hour in public aggregates) |
| `observed_date` | `DATE` | `NOT NULL` | Calendar date for recency & last-driven display |
| `traversal_duration_s` | `DOUBLE PRECISION` | `NOT NULL CHECK (traversal_duration_s > 0)` | Elapsed seconds over segment |
| `avg_speed_kmh` | `DOUBLE PRECISION` | `NOT NULL CHECK (avg_speed_kmh >= 0 AND avg_speed_kmh <= 160)` | Mean speed along segment |
| `min_speed_kmh` | `DOUBLE PRECISION` | `NULL CHECK (min_speed_kmh >= 0 AND min_speed_kmh <= 160)` | Slowest rolling speed (obstacle indicator) |
| `roughness_rms` | `DOUBLE PRECISION` | `NULL CHECK (roughness_rms >= 0)` | Optional IMU/DeviceMotion vertical RMS acceleration (`m/s²`) |
| `roughness_p95` | `DOUBLE PRECISION` | `NULL CHECK (roughness_p95 >= 0)` | Optional 95th percentile roughness spike |
| `match_confidence` | `DOUBLE PRECISION` | `NOT NULL CHECK (match_confidence >= 0 AND match_confidence <= 1)` | HMM segment confidence |
| `matched_point_count` | `INTEGER` | `NOT NULL CHECK (matched_point_count >= 1)` | GPS fixes snapped to segment |
| `slow_hotspot_geom` | `GEOMETRY(Point, 4326)` | `NULL` | Snapped obstacle/crawl location on segment (if speed `< 5 km/h` or high roughness) |
| `rainfall_72h_mm` | `DOUBLE PRECISION` | `NULL` | Concurrent 72 h precipitation snapshot at traversal time |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Ingestion timestamp |

- **Indexes:**
  - `BTREE (segment_id, observed_at DESC)`
  - `BTREE (contribution_hash)` *(fast revocation when user toggles trip to private)*
  - `BTREE (segment_id, vehicle_capability_class)`
  - `GIST (slow_hotspot_geom)` *(partial `WHERE slow_hotspot_geom IS NOT NULL`)*

#### 22. `unmapped_track_candidates` *(Slice 06 — STRICTLY ANONYMIZED, ZERO `user_id`)*
Privacy-trimmed trace portions that fail to match any active canonical segment (stored separately so unmapped trails never corrupt nearby segment ratings).

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Candidate ID |
| `contribution_hash` | `CHAR(64)` | `NOT NULL` | Revocation key (no `user_id`) |
| `vehicle_capability_class` | `TEXT` | `NOT NULL CHECK (vehicle_capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD'))` | Rig class |
| `geom` | `GEOMETRY(LineString, 4326)` | `NOT NULL` | Privacy-trimmed unmatched sub-trace |
| `length_m` | `DOUBLE PRECISION` | `NOT NULL CHECK (length_m > 0)` | Length in `EPSG:3577` |
| `observed_date` | `DATE` | `NOT NULL` | Traversal date |
| `cluster_key` | `TEXT` | `NULL` | Spatial cluster bucket for multi-user corroboration |
| `status` | `TEXT` | `NOT NULL DEFAULT 'pending_review' CHECK (status IN ('pending_review','corroborated','promoted','rejected'))` | Review lifecycle |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Created |

- **Indexes:** `GIST (geom)`, `BTREE (contribution_hash)`, `BTREE (status, cluster_key)`.

#### 23. `condition_reports` *(Slice 08 & 09)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `UUID` | `PRIMARY KEY DEFAULT gen_random_uuid()` | Report ID |
| `user_id` | `UUID` | `NOT NULL REFERENCES users(id) ON DELETE CASCADE` | Reporting user |
| `client_uuid` | `UUID` | `NULL` | Idempotent PWA offline sync key |
| `segment_id` | `TEXT` | `NULL REFERENCES track_segments(segment_id) ON DELETE RESTRICT` | Nearest canonical segment (if within 100 m) |
| `track_id` | `TEXT` | `NULL REFERENCES tracks(track_id) ON DELETE SET NULL` | Associated track |
| `report_type` | `TEXT` | `NOT NULL CHECK (report_type IN ('fallen_tree','bog_hole','washout','deep_water','locked_gate','private_property','snow_ice','track_clear'))` | Locked v1 hazard/gate enum |
| `severity` | `TEXT` | `NOT NULL DEFAULT 'caution' CHECK (severity IN ('info','caution','impassable'))` | Severity |
| `status` | `TEXT` | `NOT NULL DEFAULT 'active' CHECK (status IN ('active','resolved','stale','hidden'))` | Decay/moderation state |
| `description` | `TEXT` | `NULL` | Structured notes |
| `photo_url` | `TEXT` | `NULL` | Optional local/object-store photo path |
| `vehicle_capability_class` | `TEXT` | `NULL CHECK (vehicle_capability_class IN ('AWD','Stock 4WD','Touring 4WD','Modified 4WD'))` | Rig context when observed |
| `geom` | `GEOMETRY(Point, 4326)` | `NOT NULL` | Hazard/gate coordinate |
| `confirm_count` | `INTEGER` | `NOT NULL DEFAULT 0` | Cached confirmation votes |
| `resolve_count` | `INTEGER` | `NOT NULL DEFAULT 0` | Cached resolved votes |
| `decay_score` | `DOUBLE PRECISION` | `NOT NULL DEFAULT 1.0 CHECK (decay_score >= 0 AND decay_score <= 1)` | Dynamic staleness score (decays with age + resolves) |
| `osm_edit_url` | `TEXT` | `NULL` | Pre-filled OSM note / iD edit handoff link for `locked_gate`/`private_property` |
| `observed_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | When hazard was observed |
| `expires_at` | `TIMESTAMPTZ` | `NULL` | Auto-stale horizon (e.g. 14 d snow/water, 45 d fallen tree, 180 d locked gate) |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Created |
| `updated_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Updated |

- **Constraints:** `UNIQUE (user_id, client_uuid)`
- **Indexes:** `GIST (geom)`, `BTREE (segment_id, status, observed_at DESC)`, `BTREE (report_type, status)`, `BTREE (user_id)`.

#### 24. `condition_report_votes` *(Slice 08)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Vote ID |
| `report_id` | `UUID` | `NOT NULL REFERENCES condition_reports(id) ON DELETE CASCADE` | Target report |
| `user_id` | `UUID` | `NOT NULL REFERENCES users(id) ON DELETE CASCADE` | Voting user |
| `vote_type` | `TEXT` | `NOT NULL CHECK (vote_type IN ('confirm','resolved','inaccurate'))` | Vote action |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Vote timestamp |

- **Constraints:** `UNIQUE (report_id, user_id)`
- **Indexes:** `BTREE (report_id, vote_type)`.

#### 25. `rainfall_snapshots` *(Slice 07)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `id` | `BIGSERIAL` | `PRIMARY KEY` | Snapshot ID |
| `grid_cell_id` | `TEXT` | `NOT NULL` | `0.15°` quantized grid cell (`r_<lon2>_<lat2>`) |
| `centroid_geom` | `GEOMETRY(Point, 4326)` | `NOT NULL` | Grid cell center |
| `source_provider` | `TEXT` | `NOT NULL DEFAULT 'open-meteo'` | Weather provider |
| `rain_24h_mm` | `DOUBLE PRECISION` | `NOT NULL DEFAULT 0 CHECK (rain_24h_mm >= 0)` | Trailing 24 h rain (mm) |
| `rain_72h_mm` | `DOUBLE PRECISION` | `NOT NULL DEFAULT 0 CHECK (rain_72h_mm >= 0)` | Trailing 72 h rain (mm) |
| `rain_7d_mm` | `DOUBLE PRECISION` | `NULL CHECK (rain_7d_mm >= 0)` | Trailing 7 d rain (mm) |
| `wet_risk_level` | `TEXT` | `NOT NULL CHECK (wet_risk_level IN ('dry','damp','wet','saturated'))` | `<5 mm` dry, `5–19.9` damp, `20–49.9` wet, `>=50` saturated (72 h) |
| `fetched_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Open-Meteo fetch time |

- **Constraints:** `UNIQUE (grid_cell_id, fetched_at)`
- **Indexes:** `GIST (centroid_geom)`, `BTREE (grid_cell_id, fetched_at DESC)`.

#### 26. `offline_packs` *(Slice 09)*

| Column | Type | Constraints / Default | Notes |
| :--- | :--- | :--- | :--- |
| `pack_id` | `TEXT` | `PRIMARY KEY` | e.g. `australian-alps`, `blue-mountains`, `vic`, `nsw` |
| `name` | `TEXT` | `NOT NULL` | Display name |
| `region_type` | `TEXT` | `NOT NULL CHECK (region_type IN ('tactical','state','custom'))` | Pack tier |
| `version` | `TEXT` | `NOT NULL` | Build version/date tag (e.g. `2026.11.26`) |
| `bbox_geom` | `GEOMETRY(Polygon, 4326)` | `NOT NULL` | Pack footprint |
| `utm_zones` | `TEXT[]` | `NOT NULL DEFAULT '{}'` | Intersecting MGRS zones |
| `vector_pmtiles_path` | `TEXT` | `NOT NULL` | Relative path to vector `PMTiles` |
| `terrain_pmtiles_path` | `TEXT` | `NULL` | Relative path to Terrain-RGB `PMTiles` |
| `snapshot_json_path` | `TEXT` | `NOT NULL` | Relative path to offline tracks/POIs/closures/reports JSON |
| `size_bytes` | `BIGINT` | `NOT NULL DEFAULT 0` | Total downloadable bundle size |
| `sha256` | `CHAR(64)` | `NULL` | Manifest checksum |
| `attributions` | `JSONB` | `NOT NULL DEFAULT '[]'::jsonb` | Required attribution strings |
| `status` | `TEXT` | `NOT NULL DEFAULT 'building' CHECK (status IN ('building','ready','failed'))` | Build state |
| `built_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT NOW()` | Build completion time |

- **Indexes:** `GIST (bbox_geom)`, `BTREE (status, region_type)`.

---

## 3. Deterministic Stable Canonical Segment-ID Strategy (Slice 02 Critical Invariant)

Every downstream capability (map-matching, telemetry aggregation, live closures, community condition reports, user favorites) attaches to `track_segments.segment_id`. Re-importing updated OSM or state snapshots **must never orphan historical data**.

### 3.1 Multi-Source Conflation Priority & Deduplication Rules

All planar geometric comparisons run in **`EPSG:3577` (Australian Albers meters)**.

1. **Vehicular Eligibility Filter:**
   - Include vehicular 4WD, fire, management, and unsealed tracks (`trail_class IN ('4wd_track','fire_trail','management_trail','unsealed_road')`, `is_vehicular = TRUE`).
   - Non-vehicular walking tracks (`highway=footway`/`steps`, NSW `classsubtype=6` unless named fire/management trail, VIC `class_code IN (8,9)` or `recweb_tracks` walking-only, OSM `motor_vehicle=no` + `highway=path`) are either excluded from 4WD grading or stored with `is_vehicular = FALSE` so they never pollute 4WD HMM map-matching.
2. **Conflation Source Priority Order:**
   - **Priority 1 — OSM (`source_tracks_osm`):** Winning geometry when present (freshest GPS-aligned geometry, gates, `4wd_only`, `tracktype`, `surface`, `ford`, `bridge`).
   - **Priority 2 — VIC State (`source_tracks_vic`: `tr_road` then `recweb_tracks`):** Retained wherever OSM lacks coverage; supplies official `ezi_road_name_label` and `road_seal` enrichment onto overlapping OSM ways.
   - **Priority 3 — NSW State (`source_tracks_nsw`: `nsw_fire_trails_l9` then `nsw_tracks_l5`):** Retained wherever OSM (and border-overlapping VIC) lacks coverage; supplies official fire-trail names (`brimsid`, `roadnamebase`) and `operationalstatus` enrichment.
3. **Deduplication & Partial-Overlap Predicate (`EPSG:3577`):**
   - For each lower-priority way $S$ tested against already-accepted higher-priority geometry $P$ within `20 m`:
     - Compute corridor overlap fraction $r = \frac{\text{len}(S \cap \text{Buf}(P, 20\text{ m}))}{\text{len}(S)}$ and bidirectional acute bearing difference $\Delta\theta \in [0^\circ, 90^\circ]$.
     - **Full duplicate ($r \ge 0.70$ and $\Delta\theta \le 30^\circ$):** Drop $S$'s geometry; attach `S`'s provenance to `P`'s `source_refs JSONB`, populate `name` if `P.name` is empty (else append to `tracks.alt_names`), and fill missing `surface_class` from `S`.
     - **Partial overlap ($0.20 \le r < 0.70$ and $\Delta\theta \le 30^\circ$):** Compute $S' = \text{ST\_Difference}(S, \text{Buf}(P, 20\text{ m}))$; discard residual slivers `< 35 m`; snap remaining endpoints within `20 m` onto $P$ to form clean T-junctions; keep $S'$.
     - **Distinct / crossing ($r < 0.20$ or $\Delta\theta > 30^\circ$):** Keep $S$ in full.

### 3.2 Canonical 50–100 m Segmentation Rules

1. **Topological Noding at Junctions First:**
   - Node the conflated vehicular linework in `EPSG:3577` (`ST_Node` with `1.0 m` endpoint snap tolerance) so every intersection, fork, and T-junction is an exact node.
   - **Degree-2 Continuation Merge:** Dissolve degree-2 vertices joining two edges that share identical `(lower(coalesce(name,'')), trail_class, surface_class, access_status, primary_source)` and have deflection angle `< 45°`. This makes segmentation immune to upstream OSM mappers arbitrarily splitting a way mid-track.
2. **Canonical Edge Orientation Before Chunking:**
   - Before splitting an edge $E$ into chunks, transform endpoints $p_0, p_1$ to `EPSG:4326` and quantize to `1e-5°` (~1.1 m): $q_5(x) = \text{round}(x, 5)$.
   - Orient $E$ so `(q5(p0.lon), q5(p0.lat)) <= (q5(p1.lon), q5(p1.lat))`. If endpoints quantize equal (closed loop), orient clockwise by signed area starting at the lexicographically minimum vertex.
3. **Deterministic Equal-Length Chunking in `[50, 100]` m:**
   - Let $L = \text{ST\_Length}(E_{3577})$ in meters:
     - If $L < 15.0\text{ m}$ and dangling spur (degree-1 endpoint): discard noise sliver.
     - If $L \le 100.0\text{ m}$: $N = 1$ (keep intact; short bridge/junction connectors `15–50 m` stay single segments).
     - If $L > 100.0\text{ m}$:
       $$N_{\min} = \lceil L / 100.0 \rceil, \quad N_{\max} = \lfloor L / 50.0 \rfloor, \quad N = \min\!\big(N_{\max},\; \max(N_{\min},\; \text{round}(L / 75.0))\big)$$
       **Invariant Proof:** For every $L > 100.0$, $N_{\min} = \lceil L/100 \rceil \ge 2$ satisfies $50.0 < L / N_{\min} \le 100.0$, and $N_{\max} = \lfloor L/50 \rfloor \ge N_{\min}$ ensures $50.0 \le L / N \le 100.0$ strictly for **all** $L > 100\text{ m}$, while centering typical chunks near `75 m` to maximize margin away from `[50, 100]` boundary flips!
   - Slice $E_{3577}$ into $N$ equal fractions $i \in \{0, \dots, N-1\}$ via `ST_LineSubstring(E_3577, i / N, (i + 1) / N)` and transform each chunk back to `EPSG:4326`.

### 3.3 Direction Normalization, Quantized Fingerprint & Re-Import Spatial Reconciliation

1. **Per-Segment Direction Normalization & Fingerprint:**
   - Normalize each segment $g$ in `EPSG:4326` so `(q5(start.lon), q5(start.lat)) <= (q5(end.lon), q5(end.lat))` (tie-break using `0.25` vs `0.75` anchor points).
   - Sample 5 canonical anchor points at fractions $f \in \{0.00, 0.25, 0.50, 0.75, 1.00\}$ via `ST_LineInterpolatePoint(g_3577, f)` projected to `EPSG:4326`.
   - Build quantized string (`1e-5°` ≈ 1.1 m):
     ```text
     v1|<lon0:.5f>,<lat0:.5f>|<lon25:.5f>,<lat25:.5f>|<lon50:.5f>,<lat50:.5f>|<lon75:.5f>,<lat75:.5f>|<lon100:.5f>,<lat100:.5f>
     ```
   - `geom_fingerprint = sha256(canonical_string).hexdigest()` (64 lowercase hex chars).
   - **Deterministic `segment_id`:** `f"tfseg_{sha256(('trailforge:seg:v1|' + geom_fingerprint).encode()).hexdigest()[:16]}"`.
   - Because `segment_id` depends **only** on canonicalized geometry, upstream tag edits, way reversals, or OSM way-ID churn produce the exact same `geom_fingerprint` and preserve `segment_id` in $O(1)$.
2. **4-Pass Re-Import Reconciliation Pipeline (Zero Orphaning Under Node Jitter):**
   - **Pass 1 — Exact Fingerprint Match:** Candidate $C$ with `C.geom_fingerprint == E.geom_fingerprint` against active `track_segments` $E$ immediately reuses `E.segment_id`, updates non-key attributes, and logs `exact_match`.
   - **Pass 2 — Spatial Corridor + Hausdorff + Heading Preservation (1:1 Match):**
     - For remaining unmatched candidates $C$, query remaining active segments $E$ within `ST_DWithin(C_3577, E_3577, 15.0)`.
     - Compute in `EPSG:3577`:
       - Symmetric corridor overlap: $\text{overlap}(C, E) = \min\!\left(\frac{\text{len}(C \cap \text{Buf}(E, 12\text{ m}))}{\text{len}(C)},\; \frac{\text{len}(E \cap \text{Buf}(C, 12\text{ m}))}{\text{len}(E)}\right)$
       - Discrete Hausdorff distance: $d_H(C, E) = \text{ST\_HausdorffDistance}(C_{3577}, E_{3577})$
       - Acute bidirectional heading difference: $\Delta\theta(C, E) \in [0^\circ, 90^\circ]$
       - Length ratio: $r_L = \min(\text{len}(C), \text{len}(E)) / \max(\text{len}(C), \text{len}(E))$
     - **Preserve `E.segment_id` if:** $\text{overlap} \ge 0.75$ **AND** $d_H \le 15.0\text{ m}$ **AND** $\Delta\theta \le 25^\circ$ **AND** $r_L \ge 0.75$.
     - Resolve multiple candidates by greedy maximum-weight bipartite match on $S = 0.50\cdot\text{overlap} + 0.30\cdot(1 - d_H/15.0) + 0.20\cdot(1 - \Delta\theta/25.0)$.
     - Update `E.geom`, `E.geom_fingerprint`, elevation/slope metrics in place; log `spatial_preserved` in `segment_reconciliation_log`. All attached telemetry, grades, closures, and reports stay linked seamlessly!
   - **Pass 3 — Split / Merge / Soft-Retire Lineage:**
     - If an unmatched active $E$ has $\ge 75\%$ of its length covered by $\ge 2$ new candidates $\{C_1, \dots, C_k\}$ (e.g. a new side track creates a junction), mint deterministic IDs for $C_i$, mark $E$ `is_active = FALSE, retired_at = NOW(), superseded_by_segment_id = C_dominant.segment_id`, and log `split` rows for every `(E, C_i)` pair. Re-link active `segment_closure_links` spatially; historical `telemetry_observations` remain intact on $E$ with zero FK violations.
     - Symmetrically log `merge` (`N → 1`) and `retired` (upstream removal → soft-retire only, never `DELETE`).
   - **Pass 4 — New Segments:** Remaining unmatched candidates insert with their newly minted `tfseg_<16hex>` IDs and log `created`.

### 3.4 Explainable Day-1 Baseline Grading Formula & JSONB Schemas

Every canonical vehicular segment receives a transparent `0–100` deterministic baseline score computed solely from DEM elevation draping, normalized surface/tracktype, unbridged waterway crossings, and 4WD/access metadata.

#### Elevation Draping & Slope Calculation
- Sample DEM (`AHD` meters, bilinear interpolation) every `10 m` along `segment` in `EPSG:3577` (minimum 3 samples: `0%`, `50%`, `100%`).
- `max_slope_deg`: maximum $|\arctan(\Delta z / \Delta d)|$ (in degrees) across all pairs of sample points separated by $\Delta d \ge 20\text{ m}$ (suppresses 1-pixel SRTM noise spikes while accurately capturing steep `> 18°` pitches).
- `sustained_slope_deg`: $\max\big(\arctan(|z_{\text{end}} - z_{\text{start}}| / L),\; \text{p85}(\text{rolling 40 m slopes})\big)$.
- Set `is_steep_highlight = (max_slope_deg >= 14.0)` and `is_above_snowline = (elev_max_m >= 1000.0)`.

#### Exact Factor Weights (Max `100` Points)

| Factor | Weight | Deterministic Rules |
| :--- | :---: | :--- |
| **1. Surface & Tracktype** | `0–35` | `sealed` / `grade1` = `0`; `gravel_compacted` / `grade2` = `8`; `dirt_unsealed` / `grade3` / default `fire_trail` = `16`; `grade4` / `rough_rock_mud_sand` / unmaintained `4wd_track` = `26`; `grade5` / `smoothness IN ('very_horrible','impassable')` = `35` *(take `max(surface_pts, tracktype_pts)`)* |
| **2. Max Slope (`>= 20 m` window)** | `0–30` | `< 6°` = `0`; `6°–<10°` = `6`; `10°–<14°` = `14`; `14°–<18°` = `22`; `>= 18°` = `30` |
| **3. Sustained Slope & Relief** | `0–15` | `sustained_slope_deg`: `< 5°` = `0`, `5°–<9°` = `4`, `9°–<13°` = `8`, `>= 13°` = `12`; plus `+3` if `max(elev_gain_m, elev_loss_m) >= 12 m` on segment |
| **4. Unbridged Waterway Crossings** | `0–12` | `0` if none or `is_bridged = TRUE`; `+6` for `1` ephemeral/intermittent crossing (`+8` for `>= 2`); `+10` for perennial creek or `ford=yes` (`+12` for perennial `hierarchy='Major'` river) |
| **5. 4WD-Only & Track Class** | `0–8` | `is_4wd_only` = `8`; `is_4wd_recommended` = `5`; `trail_class='4wd_track'` = `4`; `fire_trail`/`management_trail` = `3`; `unsealed_road` = `0` *(take max)* |

#### Explainable Safety Floor Overrides (Recorded Explicitly in JSONB)
- `max_slope_deg >= 20.0` **AND** (`surface_pts >= 26` **OR** `has_perennial_crossing`) → floor score at `75.0` (`Extreme`).
- `max_slope_deg >= 18.0` **OR** (`is_4wd_only` **AND** `max_slope_deg >= 14.0`) → floor score at `50.0` (`Hard`).
- `has_perennial_crossing` **OR** `is_4wd_only` → floor score at `25.0` (`Medium`).

#### Score → Grade & Vehicle Capability Suitability Matrix (Slice 05)

| Score Range | Grade | `AWD` | `Stock 4WD` | `Touring 4WD` | `Modified 4WD` |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `0.0 – 24.9` | **`Easy`** | Suited | Suited | Suited | Suited |
| `25.0 – 49.9` | **`Medium`** | Challenging | Suited | Suited | Suited |
| `50.0 – 74.9` | **`Hard`** | High Risk | Challenging | Suited | Suited |
| `75.0 – 100.0` | **`Extreme`** | High Risk | High Risk | Challenging | Suited |

*(When `wet_risk_level IN ('wet','saturated')` on unsealed/clay/rock surfaces, suitability shifts one risk tier stricter with an explicit rainfall explanation.)*

#### Locked JSONB Schemas on `track_segment_grades`

```json
// baseline_breakdown (Slice 02)
{
  "formula_version": "baseline_v1",
  "raw_score": 87.0,
  "final_score": 87.0,
  "grade": "Extreme",
  "factors": {
    "surface_tracktype": { "points": 26, "max_points": 35, "surface_class": "rough_rock_mud_sand", "tracktype": "grade4" },
    "max_slope":         { "points": 30, "max_points": 30, "max_slope_deg": 20.4 },
    "sustained_relief":  { "points": 11, "max_points": 15, "sustained_slope_deg": 11.8, "elev_gain_m": 15.2 },
    "waterway_crossings":{ "points": 12, "max_points": 12, "unbridged_count": 1, "perennial": true, "waterways": ["Murray River"] },
    "four_wd_access":    { "points": 8,  "max_points": 8,  "is_4wd_only": true, "access_status": "public", "has_gate_barrier": false }
  },
  "overrides": ["steep_rough_or_perennial_extreme_floor"],
  "dem_tier": "5m-lidar"
}
```

```json
// live_breakdown (Slice 07 — NULL until >= 1 anonymized observation exists)
{
  "formula_version": "live_v1",
  "sample_count": 15,
  "effective_weight": 0.68,
  "confidence_level": "high",
  "last_driven_date": "2026-11-20",
  "avg_speed_kmh": 7.4,
  "avg_duration_s": 39.0,
  "p95_roughness_rms": 3.8,
  "vehicle_class_split": { "AWD": 0, "Stock 4WD": 2, "Touring 4WD": 10, "Modified 4WD": 3 },
  "obstacle_hotspot_count": 2,
  "baseline_score": 87.0,
  "telemetry_delta": 3.2,
  "blended_score": 90.2
}
```

```json
// rainfall_context (Slice 07)
{
  "provider": "open-meteo",
  "grid_cell_id": "r_148.05_-36.60",
  "rain_24h_mm": 12.4,
  "rain_72h_mm": 34.8,
  "wet_risk_level": "wet",
  "risk_note": "34.8 mm rainfall in last 72 h; expect slippery clay/rock and deeper creek crossings.",
  "fetched_at": "2026-11-26T00:00:00Z"
}
```

---

## 4. Pluggable `TrackMatcher` Protocol & Privacy-by-Design Contract

### 4.1 Pluggable `TrackMatcher` Python Protocol (`src/trailforge/matching/protocol.py`)

Map-matching sits behind a strict structural `Protocol` so Slice 06 ships a zero-broker **in-PostGIS Hidden Markov Model (HMM)** matcher (`PostGISHMMTrackMatcher`) while allowing a future `ValhallaTrackMatcher` sidecar to plug in with zero changes to telemetry ingestion or grading.

```python
from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, Sequence, runtime_checkable
from pydantic import BaseModel, Field


class GpsPoint(BaseModel):
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    timestamp: datetime
    elev_m: float | None = None
    accuracy_m: float | None = Field(default=None, ge=0.0)
    speed_mps: float | None = Field(default=None, ge=0.0)
    roughness_rms: float | None = Field(default=None, ge=0.0)


class MatchedSegmentTraversal(BaseModel):
    segment_id: str
    direction: Literal["forward", "reverse"]
    entry_at: datetime
    exit_at: datetime
    duration_s: float = Field(..., gt=0.0)
    avg_speed_kmh: float = Field(..., ge=0.0)
    min_speed_kmh: float | None = Field(default=None, ge=0.0)
    roughness_rms: float | None = Field(default=None, ge=0.0)
    roughness_p95: float | None = Field(default=None, ge=0.0)
    match_confidence: float = Field(..., ge=0.0, le=1.0)
    matched_point_count: int = Field(..., ge=1)
    slow_hotspot_lonlat: tuple[float, float] | None = None


class UnmatchedSubtrace(BaseModel):
    coordinates: list[tuple[float, float]]  # (lon, lat) in EPSG:4326
    length_m: float = Field(..., gt=0.0)
    started_at: datetime
    ended_at: datetime
    reason: Literal["no_candidates", "low_confidence", "transition_break"]


class MatchResult(BaseModel):
    matcher_name: str
    matched_segments: list[MatchedSegmentTraversal]
    unmatched_subtraces: list[UnmatchedSubtrace]
    overall_confidence: float = Field(..., ge=0.0, le=1.0)
    matched_fraction: float = Field(..., ge=0.0, le=1.0)


@runtime_checkable
class TrackMatcher(Protocol):
    """Pluggable map-matching interface (PostGIS HMM in v1; Valhalla/OSRM pluggable later)."""

    name: str

    async def match_trace(
        self,
        points: Sequence[GpsPoint],
        *,
        search_radius_m: float = 35.0,
        min_confidence: float = 0.55,
    ) -> MatchResult:
        """Snap a privacy-trimmed GPS sequence onto active vehicular canonical track_segments."""
        ...
```

#### In-PostGIS HMM Matcher Algorithm (`PostGISHMMTrackMatcher` — Slice 06)
1. **Candidate Lookup:** Batch query active vehicular `track_segments` (`is_active = TRUE AND is_vehicular = TRUE`) via GIST `ST_DWithin` in `EPSG:3577` (`search_radius_m = 35.0 m`, top `k = 5` candidates per GPS fix).
2. **Emission Probability:** Gaussian perpendicular distance likelihood $P(z_t \mid s_i) = \frac{1}{\sqrt{2\pi}\sigma_z}\exp\!\left(-\frac{d_{\perp}(z_t, s_i)^2}{2\sigma_z^2}\right)$ with $\sigma_z = \max(12.0, \text{accuracy\_m or } 12.0)\text{ m}$.
3. **Transition Probability:** Newson & Krumm exponential transition likelihood using topological adjacency (`start_node_key` / `end_node_key` graph over `track_segments`, up to 3 hops) comparing route distance $d_{\text{net}}$ vs. planar observation displacement $d_{\text{obs}}$ with $\beta = 8.0\text{ m}$.
4. **Viterbi Break & Unmapped Subtraces:** If no candidates exist within `35 m` or time/distance jump exceeds `120 s` / `300 m`, break the Viterbi chain; any contiguous unmatched run with length `>= 100 m` emits an `UnmatchedSubtrace` into `unmapped_track_candidates` rather than forcing bad snaps onto nearby segments.

### 4.2 Privacy-by-Design Contract (Mandatory Enforcement)

1. **Automatic Start/End Privacy Buffer Trimming (`250 m` Default Minimum):**
   - Before any map-matching or community persistence, compute cumulative metric distance $c_i$ in `EPSG:3577` along raw GPS points $p_0 \dots p_{M-1}$ with total length $L = c_{M-1}$.
   - Enforce `trim_m = max(250.0, trip.privacy_trim_m)`.
   - If $L \le 2 \times \text{trim\_m}$ (i.e. `<= 500 m` at default): mark `match_status = 'skipped_too_short'`, leave `trimmed_geom = NULL`, and emit **zero** `telemetry_observations` and **zero** `unmapped_track_candidates`.
   - Otherwise drop all points with $c_i < \text{trim\_m}$ and all points with $(L - c_i) < \text{trim\_m}$, linearly interpolating exact cut points at $c = \text{trim\_m}$ and $c = L - \text{trim\_m}$. Store the cropped geometry in `trips.trimmed_geom`.
2. **Identity Stripping & Revocable Private/Community Toggle:**
   - Every trip defaults to `visibility = 'private'` (private logbook only; `raw_geom` is accessible strictly to the owning `user_id`).
   - Only when `visibility = 'community_anonymous'` does the worker run `TrackMatcher` on `trimmed_geom` and write rows to `telemetry_observations` / `unmapped_track_candidates`.
   - Those public-aggregation tables store **no `user_id`** and **no `trip_id`**. They carry only `contribution_hash = HMAC_SHA256(TRAILFORGE_PRIVACY_SECRET, str(trip.contribution_nonce))`.
   - When a user toggles a trip from `community_anonymous` back to `private` (or deletes the trip):
     1. Delete all `telemetry_observations` and `unmapped_track_candidates` matching `contribution_hash`.
     2. Rotate `trip.contribution_nonce = gen_random_uuid()`.
     3. Enqueue grade recomputation for affected `segment_id`s.
   - Public endpoints expose only aggregate `track_segment_grades` statistics (and suppress speed/roughness breakdowns when `sample_count < 3` with `confidence_level = 'low'`).

---

## 5. Synthetic VIC/NSW Australian Alps Border Corridor Fixture Spec

All unit and integration CI tests run **100% offline** against a compact, realistic synthetic dataset representing the **Davies Plain / Tom Groggin / Murray River border corridor** between Victoria's Alpine National Park and NSW's Kosciuszko National Park.

### 5.1 Corridor Specification

- **Bounding Box (`EPSG:4326`):** `148.05, -36.68, 148.20, -36.55` (~`13.4 km × 14.4 km`, spanning VIC south of `-36.565` and NSW north of `-36.565`)
- **Metric CRS:** `EPSG:3577` (Australian Albers) and local UTM Zone `55S` (`EPSG:32755`, MGRS `55H FV`)
- **Elevation Range (`AHD`):** `600 m` (Murray River valley at Tom Groggin) to `1650 m` (Davies Plain Alpine Ridge)
  - Exercises the `> 1,000 m` snow-line toggle cleanly (valley `600–900 m`, mid-escarpment `900–1300 m`, alpine ridge `1300–1650 m`).
  - Includes a deliberate `19°–23°` escarpment climb on Davies Plain Track to exercise `max_slope_deg > 18°`, `is_steep_highlight = TRUE`, and `Hard`/`Extreme` baseline grades.
- **Total On-Disk Size Budget:** `< 300 KB` across all committed fixture files, deterministically reproducible via `tests/fixtures/generate_fixtures.py` (`seed=42`).

### 5.2 Exact Fixture Files Under `tests/fixtures/`

| File Path | Format / CRS | Contents & Offline Test Purpose |
| :--- | :--- | :--- |
| `tests/fixtures/generate_fixtures.py` | Python 3.12 | Deterministic generator (`seed=42`) producing every vector, raster, JSON, and GPX fixture below (pure Python + GDAL/rasterio or valid uncompressed/DEFLATE GeoTIFF writer). |
| `tests/fixtures/manifest.json` | JSON | Corridor metadata (`bbox`, `epsg:4326`, `epsg:3577`, `epsg:32755`, `elev_min_m=600`, `elev_max_m=1650`, expected feature counts & SHA-256 checksums). |
| `tests/fixtures/osm_tracks.geojson` | GeoJSON (`LineString`, `4326`) | **7 OSM ways:** `way/1001` *Tom Groggin Access Rd* (NSW, sealed/gravel `grade1`, `~2–4°` → `Easy`); `way/1002` *Davies Plain Track Lower* (VIC/NSW border, `grade4`, `surface=rock`, `4wd_only=yes`, `ford=yes` across Murray River, steep `19–22°` climb `605→1250 m` → `Extreme`); `way/1003` *Davies Plain Ridge* (VIC, `grade3`, `dirt`, `1250→1620 m` above snow-line → `Medium`/`Hard`); `way/1004` *Poplars Fire Trail* (NSW, `grade3`, `gravel`, `7–11°` `640→1080 m` → `Medium`); `way/1005` *Station Creek Private Spur* (VIC, `access=private`, `barrier=gate`); `way/1006` *Limestone Creek Bridged Link* (VIC, `bridge=yes` over Limestone Creek — verifies bridged crossing = 0 water penalty); `way/1007` *Main Range Footpad* (`highway=path`, `motor_vehicle=no` — verifies non-vehicular filter). |
| `tests/fixtures/osm_tracks_v2_jittered.geojson` | GeoJSON (`LineString`, `4326`) | Re-import invariance fixture for Slice 02: `way/1001` has tag edit only (Pass 1 exact fingerprint preservation); `way/1002` is vertex-reversed + `3–6 m` deterministic node jitter (Pass 2 direction-normalized corridor/Hausdorff preservation); `way/1004` is split mid-way at a degree-2 vertex (tests degree-2 continuation merge). |
| `tests/fixtures/vic_tracks.geojson` | GeoJSON (`MultiLineString`, `4326`) | Synthetic DataVic WFS (`tr_road` & `recweb_tracks`) with exact Vicmap fields (`pfi`, `ezi_road_name_label`, `class_code`, `road_seal`, `feature_type_code`, `source_layer`): 1 overlapping track duplicating OSM `way/1002`+`way/1003` within `6–10 m` (tests OSM>VIC conflation & name enrichment), 1 **VIC-only** fire trail (*Bucky Ridge Fire Trail*, `class_code=6`, `road_seal='2'`), and 1 `recweb_tracks` walking track. |
| `tests/fixtures/nsw_tracks.geojson` | GeoJSON (`MultiLineString`, `4326`) | Synthetic NSW Spatial Services (`NSW_Transport_Theme` layers 5 & 9) with exact NSW fields (`objectid`, `classsubtype`, `roadnamebase`, `roadnametype`, `brimsid`, `surface`, `operationalstatus`, `source_layer`): 1 layer-9 fire trail overlapping OSM `way/1004` (*Poplars Fire Trail*, `brimsid='FT-401'`), 1 **NSW-only** layer-9 fire trail (*Pinch River Fire Trail*, `brimsid='FT-409'`, `surface='2'`). |
| `tests/fixtures/dem_alps_corridor.tif` | GeoTIFF (`Float32`, `EPSG:4326`, `DEFLATE`, `NoData=-9999.0`) | **`300 × 260` grid (`0.0005°` ≈ `45–55 m` spacing, `< 100 KB` compressed):** Analytic smooth elevation surface (`600–1650 m` AHD): Murray River trough near `lat=-36.565` (`~605 m`), steep `19°–23°` escarpment rising southward (`lat=-36.585` to `-36.615`), alpine plateau `1250–1650 m` (`lat=-36.62` to `-36.67`, strictly above `1,000 m` snow-line), and NSW foothills `640–1120 m` north of `-36.565`. |
| `tests/fixtures/waterways.geojson` | GeoJSON (`LineString`, `4326`) | Synthetic GA Surface Hydrology layer 3 (`name`, `featuretype`, `perenniality`, `hierarchy`): *Murray River* (`Watercourse`, `Perennial`, `Major`, unbridged crossing of `way/1002`), *Limestone Creek* (`Watercourse`, `Intermittent`, `Minor`, bridged at `way/1006` and unbridged at *Bucky Ridge Fire Trail*), plus 1 synthetic `featuretype='Connector'` feature (tests connector exclusion). |
| `tests/fixtures/capad_parks.geojson` | GeoJSON (`MultiPolygon`, `4326`) | 2 CAPAD 2025 polygons (`NAME`, `TYPE`, `TYPE_ABBR`, `IUCN`, `STATE`): *Alpine National Park* (`VIC`, south of `-36.565`) and *Kosciuszko National Park* (`NSW`, north of `-36.565`). |
| `tests/fixtures/pois.geojson` | GeoJSON (`Point`, `4326`) | 6 POIs: *Tom Groggin Campground* (`campground`, `612 m`, `NSW`), *Davies Plain Hut* (`hut`, `1615 m`, `VIC`), duplicate *Davies Plain Hut* `35 m` away from OSM (tests POI dedupe), *Davies Trig (TS 4812)* (`trig`, `1648 m`, `VIC`), *Murray Lookout* (`lookout`, `1290 m`, `VIC`), and *Station Creek Locked Gate* (`gate`, `access=private`, `VIC`). |
| `tests/fixtures/closures_sample.json` | JSON | Offline Slice 04 mock payloads: 1 VIC MapShareVic seasonal closure (Davies Plain Track, June–Oct), 1 NSW NPWS park alert (Kosciuszko National Park), 1 VicEmergency incident polygon. |
| `tests/fixtures/sample_trace_davies_plain.gpx` | GPX 1.1 (`EPSG:4326`) | ~120 trackpoints (`~3.8 km`, `<ele>`, `<time>`, `3–8 m` GPS noise) from Tom Groggin Campground across Murray River up Davies Plain Track to Davies Plain Hut — exercises 250 m start/end privacy trimming and HMM map-matching. |
| `tests/fixtures/sample_trace_unmapped_spur.gpx` | GPX 1.1 (`EPSG:4326`) | ~45 trackpoints (`~1.2 km`) starting on Poplars Fire Trail and branching `> 100 m` off-network onto an unmapped logging spur — exercises splitting into matched traversals + `unmapped_track_candidates`. |

### 5.3 Analytic DEM Surface Formula (For Reproducible `generate_fixtures.py`)

To guarantee deterministic elevation profiles across Python/GDAL environments, `dem_alps_corridor.tif` evaluates the following closed-form `AHD` elevation function $z(\text{lon}, \text{lat})$ on the `300 × 260` grid (`origin = (148.05, -36.55)`, `dx = +0.0005°`, `dy = -0.0005°`):

```python
import math

def alps_corridor_elev_m(lon: float, lat: float) -> float:
    """Deterministic AHD elevation (600..1650 m) for the Davies Plain / Tom Groggin corridor."""
    # Murray River valley axis meanders gently around lat = -36.565
    river_lat = -36.565 + 0.004 * math.sin((lon - 148.05) * 40.0)
    d_south = river_lat - lat  # positive moving south into VIC Alpine NP

    if d_south >= 0.0:
        # Steep escarpment ramp centered around d_south = 0.032 deg (~3.5 km S of river):
        # sigmoid climb of +780 m over ~0.015 deg (~1.6 km) reaches ~19-22 deg peak slope!
        escarpment = 780.0 / (1.0 + math.exp(-(d_south - 0.032) / 0.0048))
        # Alpine ridge rise up toward Davies Plain Plateau (south-central ridge around lon=148.125)
        ridge = 240.0 * math.exp(-((lon - 148.125) / 0.055) ** 2) * min(1.0, d_south / 0.065)
        valley_grade = 600.0 + 30.0 * abs(lon - 148.12) / 0.08
        undulation = 14.0 * math.sin((lon - 148.05) * 70.0) * math.cos((lat + 36.68) * 65.0)
        elev = valley_grade + escarpment + ridge + undulation
    else:
        # North of Murray River (NSW Kosciuszko foothills): gentle climb 605..1120 m (7-11 deg)
        d_north = -d_south
        foothill = 480.0 * min(1.0, d_north / 0.015) * (0.4 + 0.6 * ((lon - 148.05) / 0.15))
        elev = 605.0 + foothill + 10.0 * math.sin((lon - 148.05) * 55.0)

    return round(max(600.0, min(1650.0, elev)), 2)
```

- **Verified properties of `alps_corridor_elev_m`:**
  - Range strictly within `[600.0, 1650.0]` m (`AHD`).
  - Valley floor at `river_lat ≈ -36.565`: `~600–640 m` (below `1,000 m` snow-line).
  - Peak escarpment gradient at `d_south = 0.032` (`lat ≈ -36.597`): $\frac{dz}{d\text{lat}} \approx \frac{780}{4 \times 0.0048} \approx 40{,}625\text{ m/deg} \approx 0.365\text{ m/m} \implies \mathbf{\arctan(0.365) \approx 20.1^\circ}$ (reliably exceeds `18°` steep threshold!).
  - Alpine ridge plateau (`lat -36.62` to `-36.67`, `lon ≈ 148.12`): `~1380–1640 m` (reliably above `1,000 m` snow-line and `1,200 m` alpine ridge criterion!).
