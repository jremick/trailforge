import {
  buildElevationProfileSvg,
  sampleElevationAtDistance,
  type ElevationChartGeometry,
  type ElevationChartResult,
  type ElevationSampleReadout,
} from "../inspectorHelpers";
import { escapeHtml } from "../presets";
import type { ElevationProfilePoint, WaterwayCrossingItem } from "../types";
import {
  COMPARE_FALLBACK_COLORS,
  sanitizeHexColor,
  type CompactCrossingItem,
  type CompactElevationPoint,
  type CompareTrackEntry,
} from "./types";

export interface CompareElevationChartResult {
  svgHtml: string;
  geometry: ElevationChartGeometry | null;
  maxDistM: number;
  minElevM: number;
  maxElevM: number;
}

/**
 * Reuse TrailForge's canonical `buildElevationProfileSvg` for `track` and `route` modes,
 * converting compact MCP wire payload types cleanly into `ElevationProfilePoint[]`.
 */
export function buildMcpElevationSvg(
  points: readonly CompactElevationPoint[],
  options: {
    width?: number;
    height?: number;
    snowLineElevationM?: number;
    crossings?: readonly CompactCrossingItem[];
  } = {}
): ElevationChartResult {
  const mappedPoints: ElevationProfilePoint[] = points.map((p) => ({
    dist_m: p.dist_m,
    elev_m: p.elev_m,
    slope_deg: p.slope_deg,
    grade: p.grade,
    segment_id: p.segment_id ?? null,
  }));

  const mappedCrossings: WaterwayCrossingItem[] = (options.crossings ?? []).map(
    (c) => ({
      name: c.name,
      perennial: c.perennial,
      bridged: c.bridged,
      dist_m: c.dist_m,
    })
  );

  return buildElevationProfileSvg(mappedPoints, {
    width: options.width ?? 680,
    height: options.height ?? 190,
    snowLineElevationM: options.snowLineElevationM ?? 1000,
    crossings: mappedCrossings,
    detailed: true,
    gradientId: "tfmElevGrad",
  });
}

/**
 * Overlaid multi-track elevation profile SVG for `compare` mode (2–4 tracks),
 * preserving TrailForge's snow-line reference band, gridlines, and scrub crosshair.
 */
export function buildCompareElevationSvg(
  tracks: readonly CompareTrackEntry[],
  options: {
    width?: number;
    height?: number;
    snowLineElevationM?: number;
    theme?: "light" | "dark";
  } = {}
): CompareElevationChartResult {
  const width = options.width ?? 680;
  const height = options.height ?? 210;
  const snowLineM = options.snowLineElevationM ?? 1000;
  const isLight = options.theme === "light";

  const validTracks = tracks.filter((t) => (t.elevation_profile ?? []).length >= 2);
  if (validTracks.length === 0) {
    return {
      svgHtml: `<div class="tfm-empty">No elevation profile samples available for comparison.</div>`,
      geometry: null,
      maxDistM: 0,
      minElevM: 0,
      maxElevM: 0,
    };
  }

  const allPts = validTracks.flatMap((t) => t.elevation_profile);
  const rawMinE = Math.min(...allPts.map((p) => p.elev_m));
  const rawMaxE = Math.max(...allPts.map((p) => p.elev_m));
  const maxDistM = Math.max(1, ...allPts.map((p) => p.dist_m));

  const spanE = Math.max(40, rawMaxE - rawMinE);
  const yMin = Math.floor((rawMinE - spanE * 0.08) / 20) * 20;
  const yMax = Math.ceil((rawMaxE + spanE * 0.1) / 20) * 20;

  const padL = 52;
  const padR = 16;
  const padT = 18;
  const padB = 28;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;
  const baseY = padT + plotH;

  const toX = (distM: number): number => padL + (distM / maxDistM) * plotW;
  const toY = (elevM: number): number =>
    padT + (1 - (elevM - yMin) / Math.max(1, yMax - yMin)) * plotH;

  const gridStroke = isLight ? "#CBD5E1" : "#233240";
  const axisText = isLight ? "#475569" : "#7F95AA";
  const axisStroke = isLight ? "#94A3B8" : "#324659";

  const yTicks: number[] = [];
  for (let i = 0; i < 5; i += 1) {
    yTicks.push(Math.round(yMin + ((yMax - yMin) * i) / 4));
  }
  const gridHtml = yTicks
    .map((e) => {
      const y = toY(e).toFixed(1);
      return `
        <line x1="${padL}" y1="${y}" x2="${width - padR}" y2="${y}" stroke="${gridStroke}" stroke-width="1" stroke-dasharray="2 2"/>
        <text x="${padL - 6}" y="${(Number(y) + 3.5).toFixed(1)}" text-anchor="end" fill="${axisText}" font-size="10.5" font-family="ui-monospace, monospace">${e}m</text>
      `;
    })
    .join("");

  const xTicksHtml: string[] = [];
  for (let i = 0; i < 6; i += 1) {
    const dist = (maxDistM * i) / 5;
    const x = toX(dist);
    const anchor = i === 0 ? "start" : i === 5 ? "end" : "middle";
    const label = i === 0 ? "0 km" : `${(dist / 1000).toFixed(1)} km`;
    xTicksHtml.push(
      `<text x="${x.toFixed(1)}" y="${height - 8}" text-anchor="${anchor}" fill="${axisText}" font-size="10.5" font-family="ui-monospace, monospace">${label}</text>`
    );
  }

  let snowHtml = "";
  if (snowLineM >= yMin && snowLineM <= yMax) {
    const sy = toY(snowLineM);
    snowHtml = `
      <rect x="${padL}" y="${padT}" width="${plotW}" height="${Math.max(0, sy - padT).toFixed(1)}" fill="rgba(125, 211, 252, 0.09)"/>
      <line x1="${padL}" y1="${sy.toFixed(1)}" x2="${width - padR}" y2="${sy.toFixed(1)}" stroke="#38BDF8" stroke-width="1.3" stroke-dasharray="5 3"/>
      <text x="${width - padR - 4}" y="${Math.max(padT + 11, sy - 4).toFixed(1)}" text-anchor="end" fill="#38BDF8" font-size="10" font-family="ui-monospace, monospace">Snow-line ${snowLineM}m</text>
    `;
  }

  const curvesHtml = validTracks
    .map((entry, idx) => {
      const color = sanitizeHexColor(
        entry.color_hex,
        COMPARE_FALLBACK_COLORS[idx % COMPARE_FALLBACK_COLORS.length]!
      );
      const coords = entry.elevation_profile
        .map((p) => `${toX(p.dist_m).toFixed(1)},${toY(p.elev_m).toFixed(1)}`)
        .join(" ");
      return `<polyline points="${coords}" fill="none" stroke="${color}" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round" data-compare-track="${escapeHtml(entry.summary.track_id)}"><title>${escapeHtml(entry.summary.name)}</title></polyline>`;
    })
    .join("");

  const svgHtml = `
    <svg class="tf-elev-svg tf-elev-svg-lg tfm-compare-elev-svg" viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="Overlaid elevation comparison across ${validTracks.length} tracks">
      ${gridHtml}
      ${snowHtml}
      ${curvesHtml}
      <line x1="${padL}" y1="${baseY.toFixed(1)}" x2="${width - padR}" y2="${baseY.toFixed(1)}" stroke="${axisStroke}" stroke-width="1"/>
      ${xTicksHtml.join("")}
      <g class="tf-elev-crosshair hidden" data-role="elev-crosshair" pointer-events="none">
        <line data-role="crosshair-line" x1="${padL}" y1="${padT}" x2="${padL}" y2="${baseY.toFixed(1)}" stroke="#FDE047" stroke-width="1.5" stroke-dasharray="3 2"/>
      </g>
      <rect class="tf-elev-scrub-hit" data-role="elev-scrub-hit" x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" fill="transparent"/>
    </svg>
  `;

  return {
    svgHtml,
    geometry: {
      width,
      height,
      padL,
      padR,
      padT,
      padB,
      plotW,
      plotH,
      yMin,
      yMax,
      totalDistM: maxDistM,
    },
    maxDistM,
    minElevM: rawMinE,
    maxElevM: rawMaxE,
  };
}

export { sampleElevationAtDistance };
export type { ElevationChartGeometry, ElevationChartResult, ElevationSampleReadout };
