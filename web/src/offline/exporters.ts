import type { Feature, FeatureCollection, LineString, Point } from "geojson";
import type { RecordedBreadcrumbPoint } from "../types";

export interface ExportWaypointInput {
  name: string;
  lon: number;
  lat: number;
  elev_m?: number | null;
  description?: string | null;
}

export interface ClientExportOptions {
  description?: string | null;
  waypoints?: readonly ExportWaypointInput[];
  metadata?: Record<string, unknown>;
}

function escapeXml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

export function slugifyExportFilename(name: string, ext: "gpx" | "geojson"): string {
  const slug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `${slug || "trailforge-track"}.${ext}`;
}

/**
 * Generate a standards-compliant, GridForge-compatible GPX 1.1 XML document
 * 100% client-side (works with zero network access).
 */
export function buildClientGpx(
  name: string,
  points: readonly Pick<
    RecordedBreadcrumbPoint,
    "lon" | "lat" | "elev_m" | "timestamp" | "roughness_rms" | "speed_mps"
  >[],
  options?: ClientExportOptions
): string {
  const cleanName = name.trim() || "TrailForge Recorded Track";
  const nowIso = new Date().toISOString();
  const lines: string[] = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<gpx version="1.1" creator="TrailForge PWA (GPL-3.0-or-later)" xmlns="http://www.topografix.com/GPX/1/1">',
    "  <metadata>",
    `    <name>${escapeXml(cleanName)}</name>`,
  ];
  if (options?.description) {
    lines.push(`    <desc>${escapeXml(options.description)}</desc>`);
  }
  lines.push(`    <time>${escapeXml(nowIso)}</time>`, "  </metadata>");

  for (const wpt of options?.waypoints ?? []) {
    if (!Number.isFinite(wpt.lat) || !Number.isFinite(wpt.lon)) continue;
    lines.push(
      `  <wpt lat="${wpt.lat.toFixed(6)}" lon="${wpt.lon.toFixed(6)}">`
    );
    if (typeof wpt.elev_m === "number" && Number.isFinite(wpt.elev_m)) {
      lines.push(`    <ele>${wpt.elev_m.toFixed(1)}</ele>`);
    }
    lines.push(`    <name>${escapeXml(wpt.name)}</name>`);
    if (wpt.description) {
      lines.push(`    <desc>${escapeXml(wpt.description)}</desc>`);
    }
    lines.push("  </wpt>");
  }

  lines.push("  <trk>", `    <name>${escapeXml(cleanName)}</name>`, "    <trkseg>");
  for (const pt of points) {
    if (!Number.isFinite(pt.lat) || !Number.isFinite(pt.lon)) continue;
    lines.push(
      `      <trkpt lat="${pt.lat.toFixed(6)}" lon="${pt.lon.toFixed(6)}">`
    );
    if (typeof pt.elev_m === "number" && Number.isFinite(pt.elev_m)) {
      lines.push(`        <ele>${pt.elev_m.toFixed(1)}</ele>`);
    }
    if (pt.timestamp) {
      lines.push(`        <time>${escapeXml(pt.timestamp)}</time>`);
    }
    const hasRough =
      typeof pt.roughness_rms === "number" && Number.isFinite(pt.roughness_rms);
    const hasSpeed =
      typeof pt.speed_mps === "number" && Number.isFinite(pt.speed_mps);
    if (hasRough || hasSpeed) {
      lines.push("        <extensions>");
      if (hasSpeed) {
        lines.push(`          <speed_mps>${pt.speed_mps!.toFixed(2)}</speed_mps>`);
      }
      if (hasRough) {
        lines.push(
          `          <roughness_rms>${pt.roughness_rms!.toFixed(4)}</roughness_rms>`
        );
      }
      lines.push("        </extensions>");
    }
    lines.push("      </trkpt>");
  }
  lines.push("    </trkseg>", "  </trk>", "</gpx>");
  return `${lines.join("\n")}\n`;
}

/**
 * Generate an RFC 7946 GeoJSON FeatureCollection (GridForge strip-route / overlay compatible)
 * 100% client-side offline.
 */
export function buildClientGeoJson(
  name: string,
  points: readonly Pick<
    RecordedBreadcrumbPoint,
    "lon" | "lat" | "elev_m" | "timestamp" | "roughness_rms" | "speed_mps"
  >[],
  options?: ClientExportOptions
): FeatureCollection {
  const cleanName = name.trim() || "TrailForge Recorded Track";
  const features: Feature[] = [];

  const validCoords = points
    .filter((p) => Number.isFinite(p.lon) && Number.isFinite(p.lat))
    .map((p) =>
      typeof p.elev_m === "number" && Number.isFinite(p.elev_m)
        ? [Number(p.lon.toFixed(6)), Number(p.lat.toFixed(6)), Number(p.elev_m.toFixed(1))]
        : [Number(p.lon.toFixed(6)), Number(p.lat.toFixed(6))]
    );

  if (validCoords.length >= 2) {
    const lineFeature: Feature<LineString> = {
      type: "Feature",
      properties: {
        name: cleanName,
        description: options?.description ?? null,
        point_count: validCoords.length,
        source: "TrailForge PWA",
        license: "GPL-3.0-or-later",
        exported_at: new Date().toISOString(),
        ...(options?.metadata ?? {}),
      },
      geometry: {
        type: "LineString",
        coordinates: validCoords,
      },
    };
    features.push(lineFeature);
  }

  for (const wpt of options?.waypoints ?? []) {
    if (!Number.isFinite(wpt.lon) || !Number.isFinite(wpt.lat)) continue;
    const ptFeature: Feature<Point> = {
      type: "Feature",
      properties: {
        name: wpt.name,
        description: wpt.description ?? null,
        elev_m: wpt.elev_m ?? null,
      },
      geometry: {
        type: "Point",
        coordinates: [Number(wpt.lon.toFixed(6)), Number(wpt.lat.toFixed(6))],
      },
    };
    features.push(ptFeature);
  }

  return {
    type: "FeatureCollection",
    features,
  };
}

export function triggerClientFileDownload(
  content: string,
  filename: string,
  mimeType: string
): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}
