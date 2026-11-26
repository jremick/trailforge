import {
  findNearestPointOnTrackCourse,
  sampleTrackCourseAtDistance,
  type TrackCoursePoint,
  type TrackCourseVertex,
} from "../inspectorHelpers";
import { escapeHtml, getGradeColor } from "../presets";
import {
  COMPARE_FALLBACK_COLORS,
  sanitizeHexColor,
  type CompactClosureItem,
  type CompactElevationPoint,
  type CompactPoiItem,
  type CompactReportItem,
  type CompactSegmentGeom,
  type TrailForgeViewPayload,
} from "./types";

export interface VectorMapRenderResult {
  svgHtml: string;
  width: number;
  height: number;
  bbox: [number, number, number, number] | null;
  scaleBarLabel: string;
  /** Distance-indexed vertices for lockstep elevation scrub sync (`track` & `route` modes). */
  vertices: TrackCourseVertex[];
  totalDistM: number;
  /** Projection helpers for dynamic overlays / pan-zoom mapping. */
  project: (lon: number, lat: number) => { x: number; y: number };
}

export interface VectorMapRenderOptions {
  width?: number;
  height?: number;
  theme?: "light" | "dark";
  selectedTrackId?: string | null;
  interactiveTracks?: boolean;
}

function haversineMeters(
  lon1: number,
  lat1: number,
  lon2: number,
  lat2: number
): number {
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad;
  const dLon = (lon2 - lon1) * rad;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 6371000 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function chooseNiceScaleBarMeters(targetM: number): { meters: number; label: string } {
  const candidates = [
    100, 200, 250, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000,
  ];
  let chosen = candidates[0]!;
  for (const c of candidates) {
    if (c <= targetM * 1.15) chosen = c;
  }
  const label = chosen >= 1000 ? `${chosen / 1000} km` : `${chosen} m`;
  return { meters: chosen, label };
}

interface DrawPolyline {
  coords: Array<[number, number]>;
  color: string;
  width: number;
  trackId?: string;
  label?: string;
  dashed?: boolean;
}

interface BboxAccumulator {
  minLon: number;
  maxLon: number;
  minLat: number;
  maxLat: number;
  hasData: boolean;
}

function createBboxAcc(): BboxAccumulator {
  return {
    minLon: Infinity,
    maxLon: -Infinity,
    minLat: Infinity,
    maxLat: -Infinity,
    hasData: false,
  };
}

function includeCoord(acc: BboxAccumulator, lon: number, lat: number): void {
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) return;
  if (lon < acc.minLon) acc.minLon = lon;
  if (lon > acc.maxLon) acc.maxLon = lon;
  if (lat < acc.minLat) acc.minLat = lat;
  if (lat > acc.maxLat) acc.maxLat = lat;
  acc.hasData = true;
}

function includeBbox(
  acc: BboxAccumulator,
  bbox: [number, number, number, number] | null | undefined
): void {
  if (!bbox) return;
  includeCoord(acc, bbox[0], bbox[1]);
  includeCoord(acc, bbox[2], bbox[3]);
}

function buildDistanceVertices(
  segments: readonly CompactSegmentGeom[],
  profile: readonly CompactElevationPoint[],
  projectX: (lon: number) => number,
  projectY: (lat: number) => number
): { vertices: TrackCourseVertex[]; totalDistM: number } {
  const segSpanMap = new Map<string, { start: number; end: number }>();
  for (const pt of profile) {
    if (!pt.segment_id) continue;
    const prev = segSpanMap.get(pt.segment_id);
    if (!prev) {
      segSpanMap.set(pt.segment_id, { start: pt.dist_m, end: pt.dist_m });
    } else {
      prev.start = Math.min(prev.start, pt.dist_m);
      prev.end = Math.max(prev.end, pt.dist_m);
    }
  }

  const vertices: TrackCourseVertex[] = [];
  let runningDistM = 0;
  let polylineId = 0;

  for (let sIdx = 0; sIdx < segments.length; sIdx += 1) {
    const seg = segments[sIdx]!;
    const coords = seg.coords ?? [];
    const segLenFallback =
      typeof seg.length_m === "number" && seg.length_m > 0 ? seg.length_m : 100;
    const span = segSpanMap.get(seg.segment_id);
    const segStart =
      sIdx === 0 && span ? Math.min(runningDistM, span.start) : runningDistM;
    const segEnd =
      span && span.end > segStart + 0.5 ? span.end : segStart + segLenFallback;

    if (coords.length < 2) {
      runningDistM = segEnd;
      continue;
    }

    polylineId += 1;
    const cumArc: number[] = [0];
    let arcTotal = 0;
    for (let i = 1; i < coords.length; i += 1) {
      const a = coords[i - 1]!;
      const b = coords[i]!;
      arcTotal += haversineMeters(a[0], a[1], b[0], b[1]);
      cumArc.push(arcTotal);
    }

    for (let i = 0; i < coords.length; i += 1) {
      const [lon, lat] = coords[i]!;
      const frac =
        arcTotal > 0 ? cumArc[i]! / arcTotal : i / Math.max(1, coords.length - 1);
      vertices.push({
        distM: segStart + frac * (segEnd - segStart),
        lon,
        lat,
        x: projectX(lon),
        y: projectY(lat),
        grade: seg.grade || "Easy",
        segmentId: seg.segment_id,
        polylineId,
      });
    }

    runningDistM = segEnd;
  }

  const profileTotal =
    profile.length > 0 ? profile[profile.length - 1]!.dist_m : 0;
  const lastVertDist =
    vertices.length > 0 ? vertices[vertices.length - 1]!.distM : 0;

  return {
    vertices,
    totalDistM: Math.max(1, profileTotal, lastVertDist),
  };
}

function poiColor(category: string): string {
  const c = category.toLowerCase();
  if (c.includes("camp")) return "#22C55E";
  if (c.includes("hut") || c.includes("shelter")) return "#F59E0B";
  if (c.includes("water")) return "#38BDF8";
  if (c.includes("fuel")) return "#EC4899";
  if (c.includes("gate") || c.includes("hazard")) return "#EF4444";
  return "#94A3B8";
}

function poiGlyph(category: string): string {
  const c = category.toLowerCase();
  if (c.includes("camp")) return "C";
  if (c.includes("hut") || c.includes("shelter")) return "H";
  if (c.includes("water")) return "W";
  if (c.includes("fuel")) return "F";
  if (c.includes("gate")) return "G";
  return "•";
}

/**
 * Render a zero-network North-up aspect-corrected interactive SVG vector map
 * supporting all 4 TrailForge MCP App view modes (`explore`, `track`, `route`, `compare`),
 * complete with TrailForge grade palette, closures, gate/hazard markers, POIs,
 * `S`/`F` badges, scale bar, north arrow, and synchronized scrub cursor.
 */
export function buildVectorMapSvg(
  payload: TrailForgeViewPayload,
  options: VectorMapRenderOptions = {}
): VectorMapRenderResult {
  const width = options.width ?? 680;
  const height = options.height ?? 340;
  const isLight = options.theme === "light";
  const interactiveTracks = Boolean(options.interactiveTracks);

  const bgFill = isLight ? "#F1F5F9" : "#0B1218";
  const gridStroke = isLight ? "#CBD5E1" : "#162330";
  const casingStroke = isLight ? "#FFFFFF" : "#060A0F";
  const chromeBg = isLight ? "rgba(255,255,255,0.9)" : "rgba(11,17,23,0.86)";
  const chromeBorder = isLight ? "#CBD5E1" : "#293B4D";
  const chromeText = isLight ? "#334155" : "#94A3B8";

  const polylines: DrawPolyline[] = [];
  const closures: CompactClosureItem[] = [];
  const reports: CompactReportItem[] = [];
  const pois: CompactPoiItem[] = [];
  const waypoints: Array<{ name: string; category: string; lon: number; lat: number }> = [];
  let primarySegments: CompactSegmentGeom[] = [];
  let primaryProfile: CompactElevationPoint[] = [];

  const acc = createBboxAcc();

  if (payload.view_mode === "track" && payload.track) {
    primarySegments = payload.track.segments ?? [];
    primaryProfile = payload.track.elevation_profile ?? [];
    for (const seg of primarySegments) {
      if (seg.coords.length >= 2) {
        polylines.push({
          coords: seg.coords,
          color: getGradeColor(seg.grade),
          width: seg.is_steep ? 5.2 : 4.2,
          trackId: payload.track.summary.track_id,
          label: payload.track.summary.name,
        });
        for (const [lon, lat] of seg.coords) includeCoord(acc, lon, lat);
      }
    }
    closures.push(...(payload.track.closures ?? []));
    reports.push(
      ...(payload.track.gate_warnings ?? []),
      ...(payload.track.condition_reports ?? [])
    );
    pois.push(...(payload.track.nearby_pois ?? []));
  } else if (payload.view_mode === "route" && payload.route) {
    primaryProfile = payload.route.elevation_profile ?? [];
    const stitchedSegments: CompactSegmentGeom[] = [];
    for (const leg of payload.route.legs ?? []) {
      for (const seg of leg.segments ?? []) {
        stitchedSegments.push(seg);
        if (seg.coords.length >= 2) {
          polylines.push({
            coords: seg.coords,
            color: getGradeColor(seg.grade),
            width: seg.is_steep ? 5.2 : 4.2,
            trackId: leg.track_id,
            label: leg.name,
          });
          for (const [lon, lat] of seg.coords) includeCoord(acc, lon, lat);
        }
      }
    }
    primarySegments = stitchedSegments;
    closures.push(...(payload.route.closures ?? []));
    reports.push(...(payload.route.gate_warnings ?? []));
    waypoints.push(...(payload.route.waypoints ?? []));
    pois.push(...(payload.route.nearby_pois ?? []));
  } else if (payload.view_mode === "compare" && payload.compare) {
    (payload.compare.tracks ?? []).forEach((entry, idx) => {
      const color = sanitizeHexColor(
        entry.color_hex,
        COMPARE_FALLBACK_COLORS[idx % COMPARE_FALLBACK_COLORS.length]!
      );
      for (const seg of entry.segments ?? []) {
        if (seg.coords.length >= 2) {
          polylines.push({
            coords: seg.coords,
            color,
            width: 4.0,
            trackId: entry.summary.track_id,
            label: entry.summary.name,
          });
          for (const [lon, lat] of seg.coords) includeCoord(acc, lon, lat);
        }
      }
      includeBbox(acc, entry.summary.bbox);
    });
  } else if (payload.explore) {
    for (const track of payload.explore.tracks ?? []) {
      const isSelected =
        options.selectedTrackId !== null &&
        options.selectedTrackId === track.track_id;
      const segs = track.segments ?? [];
      if (segs.length > 0) {
        for (const seg of segs) {
          if (seg.coords.length >= 2) {
            polylines.push({
              coords: seg.coords,
              color: getGradeColor(seg.grade || track.grade),
              width: isSelected ? 5.6 : 4.0,
              trackId: track.track_id,
              label: track.name,
            });
            for (const [lon, lat] of seg.coords) includeCoord(acc, lon, lat);
          }
        }
      } else if (track.preview_coords && track.preview_coords.length >= 2) {
        polylines.push({
          coords: track.preview_coords,
          color: getGradeColor(track.grade),
          width: isSelected ? 5.6 : 4.0,
          trackId: track.track_id,
          label: track.name,
        });
        for (const [lon, lat] of track.preview_coords) includeCoord(acc, lon, lat);
      } else if (track.center) {
        includeCoord(acc, track.center[0], track.center[1]);
      }
      includeBbox(acc, track.bbox);
    }
    closures.push(...(payload.explore.closures ?? []));
    reports.push(...(payload.explore.reports ?? []));
    pois.push(...(payload.explore.pois ?? []));
  }

  for (const c of closures) {
    if (typeof c.lon === "number" && typeof c.lat === "number") {
      includeCoord(acc, c.lon, c.lat);
    }
  }
  for (const r of reports) {
    if (typeof r.lon === "number" && typeof r.lat === "number") {
      includeCoord(acc, r.lon, r.lat);
    }
  }
  for (const p of pois) includeCoord(acc, p.lon, p.lat);
  for (const wp of waypoints) includeCoord(acc, wp.lon, wp.lat);

  if (!acc.hasData && payload.bbox) {
    includeBbox(acc, payload.bbox);
  }
  if (!acc.hasData) {
    // Fallback to Australian Alps default corridor so map is never blank
    includeBbox(acc, [148.05, -36.68, 148.2, -36.55]);
  }

  const minLon = acc.minLon;
  const maxLon = acc.maxLon;
  const minLat = acc.minLat;
  const maxLat = acc.maxLat;

  const midLatRad = (((minLat + maxLat) / 2) * Math.PI) / 180;
  const cosLat = Math.max(0.2, Math.cos(midLatRad));
  const rawW = Math.max(1e-4, (maxLon - minLon) * cosLat);
  const rawH = Math.max(1e-4, maxLat - minLat);

  const pad = 38;
  const availW = width - pad * 2;
  const availH = height - pad * 2;
  const scale = Math.min(availW / rawW, availH / rawH);
  const drawnW = rawW * scale;
  const drawnH = rawH * scale;
  const offX = pad + (availW - drawnW) / 2;
  const offY = pad + (availH - drawnH) / 2;

  const projectX = (lon: number): number => offX + (lon - minLon) * cosLat * scale;
  const projectY = (lat: number): number => offY + (maxLat - lat) * scale;
  const project = (lon: number, lat: number): { x: number; y: number } => ({
    x: projectX(lon),
    y: projectY(lat),
  });

  // Topographic graticule grid
  const gridLines: string[] = [];
  for (let i = 1; i <= 5; i += 1) {
    const gx = ((width * i) / 6).toFixed(1);
    gridLines.push(
      `<line x1="${gx}" y1="0" x2="${gx}" y2="${height}" stroke="${gridStroke}" stroke-width="1"/>`
    );
  }
  for (let i = 1; i <= 3; i += 1) {
    const gy = ((height * i) / 4).toFixed(1);
    gridLines.push(
      `<line x1="0" y1="${gy}" x2="${width}" y2="${gy}" stroke="${gridStroke}" stroke-width="1"/>`
    );
  }

  const casingHtml = polylines
    .map((line) => {
      const pts = line.coords
        .map(([lon, lat]) => `${projectX(lon).toFixed(1)},${projectY(lat).toFixed(1)}`)
        .join(" ");
      return `<polyline points="${pts}" fill="none" stroke="${casingStroke}" stroke-width="${(line.width + 3.8).toFixed(1)}" stroke-linecap="round" stroke-linejoin="round" opacity="0.9"/>`;
    })
    .join("");

  const linesHtml = polylines
    .map((line) => {
      const pts = line.coords
        .map(([lon, lat]) => `${projectX(lon).toFixed(1)},${projectY(lat).toFixed(1)}`)
        .join(" ");
      const clickAttr =
        interactiveTracks && line.trackId
          ? ` data-map-track-id="${escapeHtml(line.trackId)}" class="tfm-svg-track-line"`
          : "";
      const titleTag = line.label ? `<title>${escapeHtml(line.label)}</title>` : "";
      const safeStroke = sanitizeHexColor(line.color, "#38BDF8");
      return `<polyline points="${pts}" fill="none" stroke="${safeStroke}" stroke-width="${line.width.toFixed(1)}" stroke-linecap="round" stroke-linejoin="round"${line.dashed ? ' stroke-dasharray="6 4"' : ""}${clickAttr}>${titleTag}</polyline>`;
    })
    .join("");

  // Explore point-only track pins when segments/preview coords were omitted
  let exploreCenterPinsHtml = "";
  if (payload.view_mode === "explore" && payload.explore) {
    exploreCenterPinsHtml = (payload.explore.tracks ?? [])
      .filter(
        (t) =>
          (!t.segments || t.segments.length === 0) &&
          (!t.preview_coords || t.preview_coords.length < 2) &&
          Array.isArray(t.center)
      )
      .map((t) => {
        const cx = projectX(t.center![0]).toFixed(1);
        const cy = projectY(t.center![1]).toFixed(1);
        const color = getGradeColor(t.grade);
        const clickAttr = interactiveTracks
          ? ` data-map-track-id="${escapeHtml(t.track_id)}" class="tfm-svg-track-line"`
          : "";
        return `<g${clickAttr}><circle cx="${cx}" cy="${cy}" r="7" fill="${color}" stroke="${casingStroke}" stroke-width="2"><title>${escapeHtml(t.name)} (${escapeHtml(t.grade ?? "Unrated")})</title></circle></g>`;
      })
      .join("");
  }

  // Distance-indexed vertices for synchronized elevation scrubbing (`track` & `route`)
  const { vertices, totalDistM } = buildDistanceVertices(
    primarySegments,
    primaryProfile,
    projectX,
    projectY
  );

  // Waterway crossing markers along track
  let crossingsHtml = "";
  if (payload.view_mode === "track" && payload.track?.crossings && vertices.length >= 2) {
    crossingsHtml = payload.track.crossings
      .map((c) => {
        const pt = sampleTrackCourseAtDistance(vertices, c.dist_m);
        if (!pt) return "";
        const title = escapeHtml(
          `${c.name} (${c.perennial ? "Perennial" : "Intermittent"}, ${c.bridged ? "Bridged" : "Unbridged"})`
        );
        return `<circle cx="${pt.x.toFixed(1)}" cy="${pt.y.toFixed(1)}" r="5" fill="#38BDF8" stroke="${casingStroke}" stroke-width="1.6"><title>${title}</title></circle>`;
      })
      .join("");
  }

  // Closure markers (red/amber hazard triangles)
  const closuresHtml = closures
    .filter((c) => typeof c.lon === "number" && typeof c.lat === "number")
    .map((c) => {
      const cx = projectX(c.lon!);
      const cy = projectY(c.lat!);
      const fill = c.severity === "critical" || c.closure_type === "closed" ? "#EF4444" : "#F59E0B";
      const pts = `${cx.toFixed(1)},${(cy - 8).toFixed(1)} ${(cx + 7.5).toFixed(1)},${(cy + 6).toFixed(1)} ${(cx - 7.5).toFixed(1)},${(cy + 6).toFixed(1)}`;
      return `
        <g data-marker-kind="closure">
          <polygon points="${pts}" fill="${fill}" stroke="${casingStroke}" stroke-width="1.6"><title>${escapeHtml(c.title)}</title></polygon>
          <text x="${cx.toFixed(1)}" y="${(cy + 4).toFixed(1)}" text-anchor="middle" fill="#070B10" font-size="8.5" font-weight="800" font-family="ui-monospace, monospace">!</text>
        </g>
      `;
    })
    .join("");

  // Gate warnings & hazard reports (amber diamond)
  const reportsHtml = reports
    .filter((r) => typeof r.lon === "number" && typeof r.lat === "number")
    .map((r) => {
      const cx = projectX(r.lon!);
      const cy = projectY(r.lat!);
      const fill = r.is_gate_warning || r.report_type === "locked_gate" ? "#F59E0B" : "#FB923C";
      const pts = `${cx.toFixed(1)},${(cy - 6.5).toFixed(1)} ${(cx + 6.5).toFixed(1)},${cy.toFixed(1)} ${cx.toFixed(1)},${(cy + 6.5).toFixed(1)} ${(cx - 6.5).toFixed(1)},${cy.toFixed(1)}`;
      const label = escapeHtml(r.title || r.report_type.replace(/_/g, " "));
      return `<polygon points="${pts}" fill="${fill}" stroke="${casingStroke}" stroke-width="1.5" data-marker-kind="report"><title>${label}</title></polygon>`;
    })
    .join("");

  // POIs and route waypoints
  const poiMarkersHtml = [
    ...pois.map((p) => ({
      name: p.name,
      category: p.category,
      lon: p.lon,
      lat: p.lat,
    })),
    ...waypoints,
  ]
    .map((p) => {
      const cx = projectX(p.lon);
      const cy = projectY(p.lat);
      const col = poiColor(p.category);
      const glyph = poiGlyph(p.category);
      return `
        <g data-marker-kind="poi">
          <circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="7" fill="${col}" stroke="${casingStroke}" stroke-width="1.6"><title>${escapeHtml(p.name)} (${escapeHtml(p.category)})</title></circle>
          <text x="${cx.toFixed(1)}" y="${(cy + 3).toFixed(1)}" text-anchor="middle" fill="#071018" font-size="8.5" font-weight="800" font-family="ui-monospace, monospace">${glyph}</text>
        </g>
      `;
    })
    .join("");

  // Start (S) & Finish (F) badges for single track or stitched route
  let endpointsHtml = "";
  if (vertices.length >= 2) {
    const startPt = sampleTrackCourseAtDistance(vertices, 0) ?? vertices[0]!;
    const endPt =
      sampleTrackCourseAtDistance(vertices, totalDistM) ??
      vertices[vertices.length - 1]!;
    endpointsHtml = `
      <g pointer-events="none" data-role="sf-markers">
        <circle cx="${startPt.x.toFixed(1)}" cy="${startPt.y.toFixed(1)}" r="9" fill="#22C55E" stroke="${casingStroke}" stroke-width="2"/>
        <text x="${startPt.x.toFixed(1)}" y="${(startPt.y + 3.5).toFixed(1)}" text-anchor="middle" fill="#052E16" font-size="10" font-weight="800" font-family="ui-monospace, monospace">S</text>
        <circle cx="${endPt.x.toFixed(1)}" cy="${endPt.y.toFixed(1)}" r="9" fill="#E2E8F0" stroke="${casingStroke}" stroke-width="2"/>
        <text x="${endPt.x.toFixed(1)}" y="${(endPt.y + 3.5).toFixed(1)}" text-anchor="middle" fill="#0F172A" font-size="10" font-weight="800" font-family="ui-monospace, monospace">F</text>
      </g>
    `;
  }

  // Metric scale bar & North arrow (fixed chrome layer outside pan/zoom group)
  const metersPerPx = 111132 / scale;
  const niceBar = chooseNiceScaleBarMeters(metersPerPx * 92);
  const barWidthPx = Math.max(32, Math.min(130, niceBar.meters / metersPerPx));
  const barX = 16;
  const barY = height - 16;

  const chromeHtml = `
    <g pointer-events="none" data-role="map-chrome">
      <g transform="translate(${width - 28}, 26)" data-role="north-arrow">
        <circle cx="0" cy="0" r="14" fill="${chromeBg}" stroke="${chromeBorder}" stroke-width="1.2"/>
        <path d="M 0 -9 L 4.5 2 L 0 -1 L -4.5 2 Z" fill="#38BDF8"/>
        <text x="0" y="10.5" text-anchor="middle" fill="${chromeText}" font-size="8" font-weight="800" font-family="ui-monospace, monospace">N</text>
      </g>
      <g data-role="scale-bar">
        <rect x="${barX - 6}" y="${barY - 19}" width="${(barWidthPx + 12).toFixed(1)}" height="24" rx="4" fill="${chromeBg}" stroke="${chromeBorder}" stroke-width="1"/>
        <line x1="${barX}" y1="${barY}" x2="${(barX + barWidthPx).toFixed(1)}" y2="${barY}" stroke="${chromeText}" stroke-width="2" stroke-linecap="square"/>
        <line x1="${barX}" y1="${barY - 4.5}" x2="${barX}" y2="${barY}" stroke="${chromeText}" stroke-width="1.5"/>
        <line x1="${(barX + barWidthPx).toFixed(1)}" y1="${barY - 4.5}" x2="${(barX + barWidthPx).toFixed(1)}" y2="${barY}" stroke="${chromeText}" stroke-width="1.5"/>
        <text x="${(barX + barWidthPx / 2).toFixed(1)}" y="${barY - 6}" text-anchor="middle" fill="${chromeText}" font-size="9.5" font-weight="600" font-family="ui-monospace, monospace">${niceBar.label}</text>
      </g>
    </g>
  `;

  const initialCursorX = vertices.length > 0 ? vertices[0]!.x.toFixed(1) : "0";
  const initialCursorY = vertices.length > 0 ? vertices[0]!.y.toFixed(1) : "0";

  const cursorOverlayHtml = `
    <g class="tfm-map-cursor hidden" data-role="svg-map-cursor" pointer-events="none">
      <circle data-role="svg-cursor-glow" cx="${initialCursorX}" cy="${initialCursorY}" r="14" fill="rgba(253, 224, 71, 0.30)"/>
      <circle data-role="svg-cursor-ring" cx="${initialCursorX}" cy="${initialCursorY}" r="7.5" fill="none" stroke="#FFFFFF" stroke-width="2.2"/>
      <circle data-role="svg-cursor-dot" cx="${initialCursorX}" cy="${initialCursorY}" r="4.8" fill="#FDE047" stroke="#070B10" stroke-width="1.8"/>
    </g>
  `;

  const svgHtml = `
    <svg class="tfm-vector-map-svg" data-role="vector-map-svg" viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="North-up interactive vector trail map for ${escapeHtml(payload.title)}">
      <rect x="0" y="0" width="${width}" height="${height}" fill="${bgFill}"/>
      <g data-role="map-panzoom-layer" transform="translate(0 0) scale(1)">
        ${gridLines.join("")}
        ${casingHtml}
        ${linesHtml}
        ${exploreCenterPinsHtml}
        ${crossingsHtml}
        ${closuresHtml}
        ${reportsHtml}
        ${poiMarkersHtml}
        ${endpointsHtml}
        ${cursorOverlayHtml}
      </g>
      ${chromeHtml}
    </svg>
  `;

  return {
    svgHtml,
    width,
    height,
    bbox: [minLon, minLat, maxLon, maxLat],
    scaleBarLabel: niceBar.label,
    vertices,
    totalDistM,
    project,
  };
}

export { findNearestPointOnTrackCourse, sampleTrackCourseAtDistance };
export type { TrackCoursePoint, TrackCourseVertex };
