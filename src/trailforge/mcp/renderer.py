"""Pure-Python Pillow 2x supersampled dark-topo PNG visual renderers (Section 8.2).

Zero headless Chromium, zero Matplotlib/Cairo dependencies. Produces crisp anti-aliased
PNG charts strictly under 120 KB each:
  - Elevation Profile PNG (`960x420`)
  - Track / Route / Area Map Card PNG (`960x540`)
  - Multi-Track Comparison PNG (`960x460`)
"""

from __future__ import annotations

import io
import math
from collections.abc import Sequence
from functools import lru_cache
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from trailforge.mcp.schemas import (
    COMPARE_COLORS,
    CompactClosureItem,
    CompactCrossingItem,
    CompactElevationPoint,
    CompactPoiItem,
    CompactReportItem,
    CompactSegmentGeom,
    CompareTrackEntry,
)

ELEVATION_PNG_SIZE: Final[tuple[int, int]] = (960, 420)
MAP_CARD_PNG_SIZE: Final[tuple[int, int]] = (960, 540)
COMPARISON_PNG_SIZE: Final[tuple[int, int]] = (960, 460)
MAX_PNG_BYTES: Final[int] = 118_000  # Strictly < 120 KB (120 * 1024 = 122,880)

_SCALE: Final[int] = 2

# TrailForge Dark-Topo Palette
_BG_CANVAS: Final[tuple[int, int, int, int]] = (11, 17, 23, 255)  # #0B1117
_BG_PANEL: Final[tuple[int, int, int, int]] = (17, 25, 35, 255)  # #111923
_BG_HEADER: Final[tuple[int, int, int, int]] = (15, 23, 34, 255)  # #0F1722
_BORDER: Final[tuple[int, int, int, int]] = (30, 41, 59, 255)  # #1E293B
_GRID_LINE: Final[tuple[int, int, int, int]] = (30, 41, 59, 170)
_TEXT_PRIMARY: Final[tuple[int, int, int, int]] = (241, 245, 249, 255)  # #F1F5F9
_TEXT_MUTED: Final[tuple[int, int, int, int]] = (148, 163, 184, 255)  # #94A3B8
_TEXT_DIM: Final[tuple[int, int, int, int]] = (100, 116, 139, 255)  # #64748B
_SNOWLINE_COLOR: Final[tuple[int, int, int, int]] = (125, 211, 252, 220)  # #7DD3FC
_SNOW_SHADE: Final[tuple[int, int, int, int]] = (125, 211, 252, 24)
_STEEP_SHADE: Final[tuple[int, int, int, int]] = (239, 68, 68, 30)
_CASING_DARK: Final[tuple[int, int, int, int]] = (7, 11, 16, 235)
_ALERT_RED: Final[tuple[int, int, int, int]] = (239, 68, 68, 255)
_ALERT_AMBER: Final[tuple[int, int, int, int]] = (249, 115, 22, 255)
_POI_CAMP: Final[tuple[int, int, int, int]] = (52, 211, 153, 255)
_POI_HUT: Final[tuple[int, int, int, int]] = (251, 191, 36, 255)
_POI_WATER: Final[tuple[int, int, int, int]] = (56, 189, 248, 255)
_POI_OTHER: Final[tuple[int, int, int, int]] = (192, 132, 252, 255)

GRADE_HEX: Final[dict[str, str]] = {
    "Easy": "#22C55E",
    "Medium": "#38BDF8",
    "Hard": "#F59E0B",
    "Extreme": "#EF4444",
}

DEFAULT_ATTRIBUTION_FOOTER: Final[str] = (
    "Data: © OpenStreetMap (ODbL) · GA/DEA DEM & Hydro (CC BY 4.0) · "
    "CAPAD · State Agencies (CC BY 4.0) · Open-Meteo (CC BY 4.0)"
)

_FONT_CANDIDATES_REGULAR: Final[tuple[str, ...]] = (
    "DejaVuSans.ttf",
    "LiberationSans-Regular.ttf",
    "Arial.ttf",
    "Helvetica.ttc",
)
_FONT_CANDIDATES_BOLD: Final[tuple[str, ...]] = (
    "DejaVuSans-Bold.ttf",
    "LiberationSans-Bold.ttf",
    "Arial Bold.ttf",
    "DejaVuSans.ttf",
    "Arial.ttf",
)


def hex_to_rgba(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    """Parse ``#RRGGBB`` hex color string into an ``(R, G, B, A)`` tuple."""
    clean = hex_color.strip().lstrip("#")
    if len(clean) != 6:
        return (56, 189, 248, alpha)
    try:
        return (int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16), alpha)
    except ValueError:
        return (56, 189, 248, alpha)


def grade_rgba(grade: str | None, alpha: int = 255) -> tuple[int, int, int, int]:
    """Return RGBA tuple for a canonical TrailForge difficulty grade."""
    hex_str = GRADE_HEX.get(grade or "", GRADE_HEX["Medium"])
    return hex_to_rgba(hex_str, alpha=alpha)


@lru_cache(maxsize=64)
def _load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = _FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REGULAR
    for fname in candidates:
        try:
            return ImageFont.truetype(fname, size=size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _truncate_text(text: str, max_chars: int) -> str:
    clean = " ".join(text.split())
    if len(clean) <= max_chars:
        return clean
    return clean[: max(1, max_chars - 1)].rstrip() + "…"


def _draw_dashed_line(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    fill: tuple[int, int, int, int],
    width: int = 2,
    dash_len: int = 12,
    gap_len: int = 8,
) -> None:
    x0, y0 = start
    x1, y1 = end
    total = math.hypot(x1 - x0, y1 - y0)
    if total <= 0:
        return
    dx = (x1 - x0) / total
    dy = (y1 - y0) / total
    pos = 0.0
    while pos < total:
        seg_end = min(total, pos + dash_len)
        draw.line(
            [(x0 + dx * pos, y0 + dy * pos), (x0 + dx * seg_end, y0 + dy * seg_end)],
            fill=fill,
            width=width,
        )
        pos += dash_len + gap_len


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    text: str,
    *,
    bg: tuple[int, int, int, int],
    fg: tuple[int, int, int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    pad_x: int = 16,
    pad_y: int = 6,
    outline: tuple[int, int, int, int] | None = None,
) -> float:
    """Draw a rounded pill badge and return its right-edge x coordinate."""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    w = tw + pad_x * 2
    h = th + pad_y * 2
    radius = max(6, int(h * 0.45))
    draw.rounded_rectangle(
        [x, y, x + w, y + h],
        radius=radius,
        fill=bg,
        outline=outline,
        width=2 if outline else 0,
    )
    draw.text((x + pad_x - bbox[0], y + pad_y - bbox[1]), text, fill=fg, font=font)
    return x + w


def _draw_attribution_footer(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    footer_text: str | None = None,
) -> None:
    """Draw the mandatory dark-topo attribution footer strip at the bottom of the canvas."""
    bar_h = 30 * _SCALE
    y0 = height - bar_h
    draw.rectangle([0, y0, width, height], fill=(8, 13, 19, 255))
    draw.line([(0, y0), (width, y0)], fill=_BORDER, width=2)
    font = _load_font(10 * _SCALE)
    label = _truncate_text(footer_text or DEFAULT_ATTRIBUTION_FOOTER, 145)
    draw.text((16 * _SCALE, y0 + 8 * _SCALE), label, fill=_TEXT_DIM, font=font)


def _encode_png_under_budget(
    supersampled: Image.Image,
    target_size: tuple[int, int],
    *,
    max_bytes: int = MAX_PNG_BYTES,
) -> bytes:
    """Downscale 2x supersampled RGBA canvas and encode PNG strictly under ``max_bytes``."""
    downscaled = supersampled.resize(target_size, Image.Resampling.LANCZOS).convert("RGB")
    buf = io.BytesIO()
    downscaled.save(buf, format="PNG", optimize=True, compress_level=9)
    data = buf.getvalue()
    if len(data) <= max_bytes:
        return data

    # Fallback: clean quantized palette PNG (no dithering) preserves sharp vector lines
    for n_colors in (192, 128, 64):
        quant = downscaled.quantize(
            colors=n_colors,
            method=Image.Quantize.FASTOCTREE,
            dither=Image.Dither.NONE,
        )
        qbuf = io.BytesIO()
        quant.save(qbuf, format="PNG", optimize=True, compress_level=9)
        qdata = qbuf.getvalue()
        if len(qdata) <= max_bytes:
            return qdata
    return qdata


def _nice_step(span: float, target_ticks: int = 5) -> float:
    """Pick a human-readable axis gridline step (1, 2, 2.5, 5 × 10^n)."""
    if span <= 0 or not math.isfinite(span):
        return 1.0
    raw = span / max(1, target_ticks)
    mag = 10 ** math.floor(math.log10(raw))
    norm = raw / mag
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if norm <= mult:
            return mult * mag
    return 10.0 * mag


# =============================================================================
# 1. Elevation Profile PNG (960 x 420)
# =============================================================================


def render_elevation_profile_png(
    *,
    title: str,
    points: Sequence[CompactElevationPoint],
    grade: str | None = None,
    total_length_m: float = 0.0,
    elev_gain_m: float = 0.0,
    elev_loss_m: float = 0.0,
    max_slope_deg: float | None = None,
    snowline_m: int = 1000,
    crossings: Sequence[CompactCrossingItem] = (),
    suitability_label: str | None = None,
    footer_text: str | None = None,
) -> bytes:
    """Render a 960x420 anti-aliased dark-topo elevation profile PNG."""
    w, h = ELEVATION_PNG_SIZE[0] * _SCALE, ELEVATION_PNG_SIZE[1] * _SCALE
    img = Image.new("RGBA", (w, h), _BG_CANVAS)
    draw = ImageDraw.Draw(img, "RGBA")

    # Header bar
    header_h = 68 * _SCALE
    draw.rectangle([0, 0, w, header_h], fill=_BG_HEADER)
    draw.line([(0, header_h), (w, header_h)], fill=_BORDER, width=2)

    font_title = _load_font(17 * _SCALE, bold=True)
    font_sub = _load_font(11 * _SCALE)
    font_pill = _load_font(11 * _SCALE, bold=True)
    font_axis = _load_font(10 * _SCALE)

    draw.text(
        (18 * _SCALE, 10 * _SCALE), _truncate_text(title, 54), fill=_TEXT_PRIMARY, font=font_title
    )

    # Header badges (right-aligned or below title)
    length_km = (
        total_length_m / 1000.0
        if total_length_m > 0
        else ((points[-1].dist_m / 1000.0) if points else 0.0)
    )
    kpi_parts = [
        f"{length_km:.1f} km",
        f"+{elev_gain_m:.0f}m / -{elev_loss_m:.0f}m",
    ]
    if max_slope_deg is not None:
        kpi_parts.append(f"Max slope {max_slope_deg:.1f}°")
    if crossings:
        unbridged = sum(1 for c in crossings if not c.bridged)
        kpi_parts.append(
            f"{len(crossings)} crossing(s)" + (f" ({unbridged} unbridged)" if unbridged else "")
        )
    draw.text(
        (18 * _SCALE, 38 * _SCALE), "   ·   ".join(kpi_parts), fill=_TEXT_MUTED, font=font_sub
    )

    badge_x = w - 20 * _SCALE
    if suitability_label:
        sbbox = draw.textbbox((0, 0), suitability_label, font=font_pill)
        sw = (sbbox[2] - sbbox[0]) + 28 * _SCALE
        badge_x -= sw
        suit_color = (
            (34, 197, 94, 48)
            if "Suited" in suitability_label
            else (245, 158, 11, 48)
            if "Challenging" in suitability_label
            else (239, 68, 68, 55)
        )
        suit_fg = (
            (134, 239, 172, 255)
            if "Suited" in suitability_label
            else (253, 224, 71, 255)
            if "Challenging" in suitability_label
            else (252, 165, 165, 255)
        )
        _draw_pill(
            draw,
            badge_x,
            16 * _SCALE,
            suitability_label,
            bg=suit_color,
            fg=suit_fg,
            font=font_pill,
            pad_x=14 * _SCALE,
            pad_y=6 * _SCALE,
            outline=suit_fg,
        )
        badge_x -= 10 * _SCALE

    if grade:
        gbbox = draw.textbbox((0, 0), grade, font=font_pill)
        gw = (gbbox[2] - gbbox[0]) + 28 * _SCALE
        badge_x -= gw
        gc = grade_rgba(grade, 255)
        _draw_pill(
            draw,
            badge_x,
            16 * _SCALE,
            grade,
            bg=(gc[0], gc[1], gc[2], 46),
            fg=gc,
            font=font_pill,
            pad_x=14 * _SCALE,
            pad_y=6 * _SCALE,
            outline=gc,
        )

    # Plot frame
    pad_l = 66 * _SCALE
    pad_r = 26 * _SCALE
    pad_t = header_h + 20 * _SCALE
    pad_b = 58 * _SCALE
    plot_x0, plot_y0 = pad_l, pad_t
    plot_x1, plot_y1 = w - pad_r, h - pad_b
    plot_w = max(1, plot_x1 - plot_x0)
    plot_h = max(1, plot_y1 - plot_y0)

    draw.rounded_rectangle(
        [plot_x0, plot_y0, plot_x1, plot_y1],
        radius=8 * _SCALE,
        fill=_BG_PANEL,
        outline=_BORDER,
        width=2,
    )

    valid_pts = [p for p in points if math.isfinite(p.dist_m) and math.isfinite(p.elev_m)]

    if len(valid_pts) < 2:
        msg = "No elevation profile samples available for this track"
        mbbox = draw.textbbox((0, 0), msg, font=font_sub)
        mw = mbbox[2] - mbbox[0]
        mh = mbbox[3] - mbbox[1]
        draw.text(
            (plot_x0 + (plot_w - mw) / 2, plot_y0 + (plot_h - mh) / 2),
            msg,
            fill=_TEXT_MUTED,
            font=font_sub,
        )
        _draw_attribution_footer(draw, w, h, footer_text)
        return _encode_png_under_budget(img, ELEVATION_PNG_SIZE)

    max_dist = max(valid_pts[-1].dist_m, max(p.dist_m for p in valid_pts), 1.0)
    raw_min_e = min(p.elev_m for p in valid_pts)
    raw_max_e = max(p.elev_m for p in valid_pts)
    span_e = max(40.0, raw_max_e - raw_min_e)
    elev_lo = max(0.0, math.floor((raw_min_e - span_e * 0.14) / 20.0) * 20.0)
    elev_hi = math.ceil((raw_max_e + span_e * 0.16) / 20.0) * 20.0
    if elev_hi <= elev_lo + 10.0:
        elev_hi = elev_lo + 50.0

    def to_px(dist_m: float, elev_m: float) -> tuple[float, float]:
        x = plot_x0 + (max(0.0, min(max_dist, dist_m)) / max_dist) * plot_w
        frac_y = (elev_m - elev_lo) / (elev_hi - elev_lo)
        y = plot_y1 - max(0.0, min(1.0, frac_y)) * plot_h
        return (x, y)

    # Snowline translucent zone + dashed horizontal line when within/near vertical range
    if elev_lo < snowline_m < elev_hi:
        _, snow_y = to_px(0.0, float(snowline_m))
        draw.rectangle([plot_x0 + 2, plot_y0 + 2, plot_x1 - 2, snow_y], fill=_SNOW_SHADE)
        _draw_dashed_line(
            draw,
            (plot_x0 + 2, snow_y),
            (plot_x1 - 2, snow_y),
            fill=_SNOWLINE_COLOR,
            width=3,
            dash_len=14,
            gap_len=10,
        )
        snow_lbl = f"Snowline {snowline_m} m"
        sb = draw.textbbox((0, 0), snow_lbl, font=font_axis)
        draw.text(
            (
                plot_x1 - (sb[2] - sb[0]) - 10 * _SCALE,
                max(plot_y0 + 4 * _SCALE, snow_y - 16 * _SCALE),
            ),
            snow_lbl,
            fill=_SNOWLINE_COLOR,
            font=font_axis,
        )

    # Horizontal elevation gridlines + Y labels
    y_step = _nice_step(elev_hi - elev_lo, target_ticks=5)
    y_tick = math.ceil(elev_lo / y_step) * y_step
    while y_tick <= elev_hi + 1e-6:
        _, gy = to_px(0.0, y_tick)
        if plot_y0 + 4 <= gy <= plot_y1 - 4:
            draw.line([(plot_x0 + 2, gy), (plot_x1 - 2, gy)], fill=_GRID_LINE, width=2)
        lbl = f"{int(round(y_tick))} m"
        lb = draw.textbbox((0, 0), lbl, font=font_axis)
        draw.text(
            (plot_x0 - (lb[2] - lb[0]) - 10 * _SCALE, gy - (lb[3] - lb[1]) / 2 - lb[1]),
            lbl,
            fill=_TEXT_MUTED,
            font=font_axis,
        )
        y_tick += y_step

    # Vertical distance gridlines + X labels
    max_km = max_dist / 1000.0
    x_step_km = _nice_step(max_km, target_ticks=6)
    x_km = 0.0
    while x_km <= max_km + 1e-6:
        gx, _ = to_px(x_km * 1000.0, elev_lo)
        if plot_x0 + 4 <= gx <= plot_x1 - 4:
            draw.line([(gx, plot_y0 + 2), (gx, plot_y1 - 2)], fill=(30, 41, 59, 100), width=2)
        lbl = f"{x_km:.1f} km" if x_step_km < 1.0 else f"{x_km:.0f} km"
        lb = draw.textbbox((0, 0), lbl, font=font_axis)
        lx = max(plot_x0, min(plot_x1 - (lb[2] - lb[0]), gx - (lb[2] - lb[0]) / 2))
        draw.text((lx, plot_y1 + 6 * _SCALE), lbl, fill=_TEXT_MUTED, font=font_axis)
        x_km += x_step_km

    # Steep section vertical bands (>= 14 deg) + grade-colored area fills
    px_pts = [to_px(p.dist_m, p.elev_m) for p in valid_pts]
    for i in range(len(valid_pts) - 1):
        p0, p1 = valid_pts[i], valid_pts[i + 1]
        x0, y0 = px_pts[i]
        x1, y1 = px_pts[i + 1]
        seg_grade = p0.grade or p1.grade or grade or "Medium"
        if max(p0.slope_deg, p1.slope_deg) >= 14.0 and x1 > x0:
            draw.rectangle([x0, plot_y0 + 2, x1, plot_y1 - 2], fill=_STEEP_SHADE)
        fill_col = grade_rgba(seg_grade, 68)
        draw.polygon([(x0, plot_y1 - 2), (x0, y0), (x1, y1), (x1, plot_y1 - 2)], fill=fill_col)

    # Thick grade-colored profile line
    for i in range(len(valid_pts) - 1):
        seg_grade = valid_pts[i].grade or valid_pts[i + 1].grade or grade or "Medium"
        draw.line(
            [px_pts[i], px_pts[i + 1]], fill=grade_rgba(seg_grade, 255), width=6, joint="curve"
        )

    # Steep peak callout badge if track has >= 14 deg slope
    steepest_idx = max(range(len(valid_pts)), key=lambda idx: valid_pts[idx].slope_deg)
    if valid_pts[steepest_idx].slope_deg >= 14.0:
        sx, sy = px_pts[steepest_idx]
        s_lbl = f"Peak {valid_pts[steepest_idx].slope_deg:.0f}°"
        draw.ellipse(
            [sx - 8, sy - 8, sx + 8, sy + 8], fill=_ALERT_RED, outline=_TEXT_PRIMARY, width=3
        )
        sb = draw.textbbox((0, 0), s_lbl, font=font_axis)
        lx = max(plot_x0 + 8, min(plot_x1 - (sb[2] - sb[0]) - 16, sx - (sb[2] - sb[0]) / 2))
        ly = max(plot_y0 + 8, sy - 34)
        _draw_pill(
            draw,
            lx,
            ly,
            s_lbl,
            bg=(15, 23, 34, 225),
            fg=(252, 165, 165, 255),
            font=font_axis,
            pad_x=8,
            pad_y=4,
            outline=_ALERT_RED,
        )

    # Waterway crossing pins along profile
    for crossing in crossings[:12]:
        cx, _ = to_px(crossing.dist_m, elev_lo)
        # Interpolate elevation at crossing dist
        cy = plot_y1 - 18
        for i in range(len(valid_pts) - 1):
            d0, d1 = valid_pts[i].dist_m, valid_pts[i + 1].dist_m
            if d0 <= crossing.dist_m <= d1 and d1 > d0:
                frac = (crossing.dist_m - d0) / (d1 - d0)
                e_interp = (
                    valid_pts[i].elev_m + (valid_pts[i + 1].elev_m - valid_pts[i].elev_m) * frac
                )
                _, cy = to_px(crossing.dist_m, e_interp)
                break
        pin_col = _POI_WATER if crossing.bridged else (6, 182, 212, 255)
        ring_col = _TEXT_PRIMARY if crossing.bridged else _ALERT_AMBER
        draw.line([(cx, cy), (cx, plot_y1 - 2)], fill=(56, 189, 248, 120), width=2)
        draw.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=pin_col, outline=ring_col, width=3)

    _draw_attribution_footer(draw, w, h, footer_text)
    return _encode_png_under_budget(img, ELEVATION_PNG_SIZE)


# =============================================================================
# 2. Track / Route / Area Map Card PNG (960 x 540)
# =============================================================================


def _compute_nice_scale_bar(
    min_lon: float, max_lon: float, mid_lat: float, plot_w_px: float
) -> tuple[float, str]:
    """Return ``(bar_width_px, label)`` for an aspect-corrected metric scale bar."""
    meters_per_deg_lon = 111_320.0 * max(0.2, math.cos(math.radians(mid_lat)))
    span_m = max(100.0, (max_lon - min_lon) * meters_per_deg_lon)
    target_m = span_m * 0.22
    candidates = [100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000]
    chosen = min(candidates, key=lambda c: abs(c - target_m))
    bar_px = max(60.0, min(plot_w_px * 0.35, (chosen / span_m) * plot_w_px))
    label = f"{chosen / 1000:.0f} km" if chosen >= 1000 else f"{chosen} m"
    return bar_px, label


def _rects_overlap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def render_map_card_png(
    *,
    title: str,
    subtitle: str | None = None,
    segments: Sequence[CompactSegmentGeom] = (),
    bbox: Sequence[float] | None = None,
    closures: Sequence[CompactClosureItem] = (),
    reports: Sequence[CompactReportItem] = (),
    pois: Sequence[CompactPoiItem] = (),
    show_endpoints: bool = True,
    footer_text: str | None = None,
) -> bytes:
    """Render a 960x540 aspect-corrected dark-topo Track / Route / Area map card PNG."""
    w, h = MAP_CARD_PNG_SIZE[0] * _SCALE, MAP_CARD_PNG_SIZE[1] * _SCALE
    img = Image.new("RGBA", (w, h), _BG_CANVAS)
    draw = ImageDraw.Draw(img, "RGBA")

    header_h = 64 * _SCALE
    footer_h = 30 * _SCALE
    draw.rectangle([0, 0, w, header_h], fill=_BG_HEADER)
    draw.line([(0, header_h), (w, header_h)], fill=_BORDER, width=2)

    font_title = _load_font(17 * _SCALE, bold=True)
    font_sub = _load_font(11 * _SCALE)
    font_lbl = _load_font(10 * _SCALE, bold=True)
    font_small = _load_font(9 * _SCALE)

    draw.text(
        (18 * _SCALE, 10 * _SCALE), _truncate_text(title, 62), fill=_TEXT_PRIMARY, font=font_title
    )
    if subtitle:
        draw.text(
            (18 * _SCALE, 37 * _SCALE),
            _truncate_text(subtitle, 96),
            fill=_TEXT_MUTED,
            font=font_sub,
        )

    map_x0 = 16 * _SCALE
    map_y0 = header_h + 12 * _SCALE
    map_x1 = w - 16 * _SCALE
    map_y1 = h - footer_h - 12 * _SCALE
    map_w = max(1, map_x1 - map_x0)
    map_h = max(1, map_y1 - map_y0)

    draw.rounded_rectangle(
        [map_x0, map_y0, map_x1, map_y1],
        radius=8 * _SCALE,
        fill=_BG_PANEL,
        outline=_BORDER,
        width=2,
    )

    # Determine bounds from segments, markers, or explicit bbox
    lons: list[float] = []
    lats: list[float] = []
    for seg in segments:
        for pt in seg.coords:
            if len(pt) >= 2 and math.isfinite(pt[0]) and math.isfinite(pt[1]):
                lons.append(float(pt[0]))
                lats.append(float(pt[1]))
    for poi in pois:
        if math.isfinite(poi.lon) and math.isfinite(poi.lat):
            lons.append(poi.lon)
            lats.append(poi.lat)
    for cl in closures:
        if (
            cl.lon is not None
            and cl.lat is not None
            and math.isfinite(cl.lon)
            and math.isfinite(cl.lat)
        ):
            lons.append(cl.lon)
            lats.append(cl.lat)
    for rep in reports:
        if (
            rep.lon is not None
            and rep.lat is not None
            and math.isfinite(rep.lon)
            and math.isfinite(rep.lat)
        ):
            lons.append(rep.lon)
            lats.append(rep.lat)

    if lons and lats:
        min_lon, max_lon = min(lons), max(lons)
        min_lat, max_lat = min(lats), max(lats)
    elif bbox and len(bbox) == 4 and all(math.isfinite(float(v)) for v in bbox):
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    else:
        min_lon, min_lat, max_lon, max_lat = 148.05, -36.68, 148.20, -36.55

    # Expand degenerate bounds slightly so single points or short lines render cleanly
    if max_lon - min_lon < 0.004:
        mid = (min_lon + max_lon) / 2.0
        min_lon, max_lon = mid - 0.003, mid + 0.003
    if max_lat - min_lat < 0.004:
        mid = (min_lat + max_lat) / 2.0
        min_lat, max_lat = mid - 0.003, mid + 0.003

    mid_lat = (min_lat + max_lat) / 2.0
    cos_lat = max(0.2, math.cos(math.radians(mid_lat)))

    # Aspect-corrected equirectangular fit inside map viewport with 12% margin
    inner_pad = 32 * _SCALE
    avail_w = max(1.0, map_w - inner_pad * 2)
    avail_h = max(1.0, map_h - inner_pad * 2)
    geo_w = (max_lon - min_lon) * cos_lat
    geo_h = max_lat - min_lat
    px_per_deg = min(avail_w / geo_w, avail_h / geo_h)
    center_lon = (min_lon + max_lon) / 2.0
    center_lat = (min_lat + max_lat) / 2.0
    view_cx = (map_x0 + map_x1) / 2.0
    view_cy = (map_y0 + map_y1) / 2.0

    # Effective viewport geographic span for graticule + scale bar
    view_span_lon = map_w / (px_per_deg * cos_lat)
    view_span_lat = map_h / px_per_deg
    v_min_lon = center_lon - view_span_lon / 2.0
    v_max_lon = center_lon + view_span_lon / 2.0
    v_min_lat = center_lat - view_span_lat / 2.0
    v_max_lat = center_lat + view_span_lat / 2.0

    def proj(lon: float, lat: float) -> tuple[float, float]:
        x = view_cx + (lon - center_lon) * cos_lat * px_per_deg
        y = view_cy - (lat - center_lat) * px_per_deg
        return (x, y)

    # Subtle graticule gridlines
    lon_step = _nice_step(view_span_lon, target_ticks=5)
    lat_step = _nice_step(view_span_lat, target_ticks=4)
    g_lon = math.ceil(v_min_lon / lon_step) * lon_step
    while g_lon <= v_max_lon:
        gx, _ = proj(g_lon, center_lat)
        if map_x0 + 8 < gx < map_x1 - 8:
            draw.line([(gx, map_y0 + 2), (gx, map_y1 - 2)], fill=(30, 41, 59, 105), width=2)
            draw.text((gx + 4, map_y0 + 6), f"{g_lon:.2f}°E", fill=_TEXT_DIM, font=font_small)
        g_lon += lon_step

    g_lat = math.ceil(v_min_lat / lat_step) * lat_step
    while g_lat <= v_max_lat:
        _, gy = proj(center_lon, g_lat)
        if map_y0 + 18 < gy < map_y1 - 22:
            draw.line([(map_x0 + 2, gy), (map_x1 - 2, gy)], fill=(30, 41, 59, 105), width=2)
            lat_lbl = f"{abs(g_lat):.2f}°{'S' if g_lat < 0 else 'N'}"
            draw.text((map_x0 + 8, gy - 16), lat_lbl, fill=_TEXT_DIM, font=font_small)
        g_lat += lat_step

    # Draw track segments: dark casing first, then vibrant grade core
    first_coord: tuple[float, float] | None = None
    last_coord: tuple[float, float] | None = None
    for seg in segments:
        if len(seg.coords) < 2:
            continue
        pts = [proj(float(c[0]), float(c[1])) for c in seg.coords]
        if first_coord is None:
            first_coord = pts[0]
        last_coord = pts[-1]
        draw.line(pts, fill=_CASING_DARK, width=14, joint="curve")

    for seg in segments:
        if len(seg.coords) < 2:
            continue
        pts = [proj(float(c[0]), float(c[1])) for c in seg.coords]
        draw.line(pts, fill=grade_rgba(seg.grade, 255), width=8, joint="curve")

    # Track occupied label rects for greedy collision avoidance
    occupied: list[tuple[float, float, float, float]] = [
        (map_x1 - 90 * _SCALE, map_y0, map_x1, map_y0 + 90 * _SCALE),  # north arrow reserve
        (map_x0, map_y1 - 60 * _SCALE, map_x0 + 240 * _SCALE, map_y1),  # scale bar reserve
        (map_x1 - 310 * _SCALE, map_y1 - 54 * _SCALE, map_x1, map_y1),  # legend reserve
    ]

    def try_place_label(
        px: float,
        py: float,
        text: str,
        *,
        fg: tuple[int, int, int, int],
        border: tuple[int, int, int, int],
    ) -> None:
        short = _truncate_text(text, 26)
        tb = draw.textbbox((0, 0), short, font=font_small)
        lw = (tb[2] - tb[0]) + 16
        lh = (tb[3] - tb[1]) + 10
        offsets = [(14, -lh / 2), (-lw - 14, -lh / 2), (-lw / 2, -lh - 14), (-lw / 2, 14)]
        for ox, oy in offsets:
            rx0, ry0 = px + ox, py + oy
            rx1, ry1 = rx0 + lw, ry0 + lh
            if rx0 < map_x0 + 6 or rx1 > map_x1 - 6 or ry0 < map_y0 + 6 or ry1 > map_y1 - 6:
                continue
            rect = (rx0 - 4, ry0 - 4, rx1 + 4, ry1 + 4)
            if any(_rects_overlap(rect, occ) for occ in occupied):
                continue
            occupied.append(rect)
            draw.rounded_rectangle(
                [rx0, ry0, rx1, ry1],
                radius=6,
                fill=(11, 17, 23, 220),
                outline=border,
                width=2,
            )
            draw.text((rx0 + 8 - tb[0], ry0 + 5 - tb[1]), short, fill=fg, font=font_small)
            return

    # POIs
    for poi in pois[:16]:
        px, py = proj(poi.lon, poi.lat)
        if not (map_x0 <= px <= map_x1 and map_y0 <= py <= map_y1):
            continue
        pcol = (
            _POI_CAMP
            if poi.poi_type == "campground"
            else _POI_HUT
            if poi.poi_type == "hut"
            else _POI_WATER
            if poi.poi_type == "water_point"
            else _POI_OTHER
        )
        draw.ellipse([px - 9, py - 9, px + 9, py + 9], fill=pcol, outline=_CASING_DARK, width=3)
        try_place_label(px, py, poi.name, fg=_TEXT_PRIMARY, border=pcol)

    # Community gate / hazard report markers
    for rep in reports[:10]:
        if rep.lon is None or rep.lat is None:
            continue
        rx, ry = proj(rep.lon, rep.lat)
        if not (map_x0 <= rx <= map_x1 and map_y0 <= ry <= map_y1):
            continue
        col = _ALERT_RED if rep.severity == "impassable" else _ALERT_AMBER
        draw.polygon(
            [(rx, ry - 13), (rx + 12, ry + 10), (rx - 12, ry + 10)], fill=col, outline=_TEXT_PRIMARY
        )
        try_place_label(rx, ry, rep.title, fg=(254, 215, 170, 255), border=col)

    # Active agency closures
    for cl in closures[:10]:
        if cl.lon is None or cl.lat is None:
            continue
        cx, cy = proj(cl.lon, cl.lat)
        if not (map_x0 <= cx <= map_x1 and map_y0 <= cy <= map_y1):
            continue
        draw.ellipse(
            [cx - 14, cy - 14, cx + 14, cy + 14], fill=_ALERT_RED, outline=_TEXT_PRIMARY, width=3
        )
        draw.text((cx - 4, cy - 11), "!", fill=_TEXT_PRIMARY, font=font_lbl)
        try_place_label(cx, cy, cl.title, fg=(254, 202, 202, 255), border=_ALERT_RED)

    # Start (S) & Finish (F) circular badges
    if show_endpoints and first_coord is not None and last_coord is not None:
        for pt, letter, bg in (
            (first_coord, "S", (22, 163, 74, 255)),
            (last_coord, "F", (2, 132, 199, 255)),
        ):
            bx, by = pt
            r = 15
            draw.ellipse([bx - r, by - r, bx + r, by + r], fill=bg, outline=_TEXT_PRIMARY, width=4)
            lb = draw.textbbox((0, 0), letter, font=font_lbl)
            draw.text(
                (bx - (lb[2] - lb[0]) / 2 - lb[0], by - (lb[3] - lb[1]) / 2 - lb[1]),
                letter,
                fill=_TEXT_PRIMARY,
                font=font_lbl,
            )

    if not segments and not pois and not closures and not reports:
        msg = "No mapped track geometries in selected area"
        mb = draw.textbbox((0, 0), msg, font=font_sub)
        draw.text(
            (view_cx - (mb[2] - mb[0]) / 2, view_cy - (mb[3] - mb[1]) / 2),
            msg,
            fill=_TEXT_MUTED,
            font=font_sub,
        )

    # North Arrow (top-right inside map panel)
    nx = map_x1 - 38 * _SCALE
    ny = map_y0 + 38 * _SCALE
    draw.ellipse(
        [nx - 24, ny - 24, nx + 24, ny + 24], fill=(11, 17, 23, 210), outline=_BORDER, width=2
    )
    draw.polygon([(nx, ny - 16), (nx + 9, ny + 10), (nx, ny + 4)], fill=_TEXT_PRIMARY)
    draw.polygon([(nx, ny - 16), (nx - 9, ny + 10), (nx, ny + 4)], fill=_TEXT_MUTED)
    nb = draw.textbbox((0, 0), "N", font=font_small)
    draw.text((nx - (nb[2] - nb[0]) / 2, ny + 18), "N", fill=_TEXT_PRIMARY, font=font_small)

    # Scale bar (bottom-left inside map panel)
    bar_px, bar_label = _compute_nice_scale_bar(v_min_lon, v_max_lon, mid_lat, float(map_w))
    sx0 = map_x0 + 20 * _SCALE
    sy0 = map_y1 - 24 * _SCALE
    draw.rounded_rectangle(
        [sx0 - 10, sy0 - 24, sx0 + bar_px + 20, sy0 + 16],
        radius=8,
        fill=(11, 17, 23, 210),
        outline=_BORDER,
        width=2,
    )
    draw.line([(sx0, sy0), (sx0 + bar_px, sy0)], fill=_TEXT_PRIMARY, width=4)
    draw.line([(sx0, sy0 - 7), (sx0, sy0 + 7)], fill=_TEXT_PRIMARY, width=4)
    draw.line([(sx0 + bar_px, sy0 - 7), (sx0 + bar_px, sy0 + 7)], fill=_TEXT_PRIMARY, width=4)
    draw.text((sx0, sy0 - 22), bar_label, fill=_TEXT_PRIMARY, font=font_small)

    # Grade Legend (bottom-right inside map panel)
    leg_w = 278 * _SCALE
    leg_h = 30 * _SCALE
    lx0 = map_x1 - leg_w - 14 * _SCALE
    ly0 = map_y1 - leg_h - 14 * _SCALE
    draw.rounded_rectangle(
        [lx0, ly0, lx0 + leg_w, ly0 + leg_h],
        radius=8,
        fill=(11, 17, 23, 215),
        outline=_BORDER,
        width=2,
    )
    cursor_x = lx0 + 14 * _SCALE
    for g_name in ("Easy", "Medium", "Hard", "Extreme"):
        gc = grade_rgba(g_name, 255)
        draw.ellipse(
            [cursor_x, ly0 + 10 * _SCALE, cursor_x + 10 * _SCALE, ly0 + 20 * _SCALE], fill=gc
        )
        draw.text(
            (cursor_x + 14 * _SCALE, ly0 + 8 * _SCALE), g_name, fill=_TEXT_PRIMARY, font=font_small
        )
        tb = draw.textbbox((0, 0), g_name, font=font_small)
        cursor_x += (tb[2] - tb[0]) + 26 * _SCALE

    _draw_attribution_footer(draw, w, h, footer_text)
    return _encode_png_under_budget(img, MAP_CARD_PNG_SIZE)


# =============================================================================
# 3. Multi-Track Comparison PNG (960 x 460)
# =============================================================================


def render_comparison_png(
    *,
    title: str,
    tracks: Sequence[CompareTrackEntry],
    snowline_m: int = 1000,
    footer_text: str | None = None,
) -> bytes:
    """Render a 960x460 overlaid elevation curves + KPI strips PNG comparing 2-4 tracks."""
    w, h = COMPARISON_PNG_SIZE[0] * _SCALE, COMPARISON_PNG_SIZE[1] * _SCALE
    img = Image.new("RGBA", (w, h), _BG_CANVAS)
    draw = ImageDraw.Draw(img, "RGBA")

    header_h = 56 * _SCALE
    footer_h = 30 * _SCALE
    kpi_strip_h = 112 * _SCALE
    draw.rectangle([0, 0, w, header_h], fill=_BG_HEADER)
    draw.line([(0, header_h), (w, header_h)], fill=_BORDER, width=2)

    font_title = _load_font(16 * _SCALE, bold=True)
    font_sub = _load_font(11 * _SCALE)
    font_card = _load_font(10 * _SCALE, bold=True)
    font_small = _load_font(9 * _SCALE)

    draw.text(
        (18 * _SCALE, 14 * _SCALE), _truncate_text(title, 66), fill=_TEXT_PRIMARY, font=font_title
    )

    plot_x0 = 66 * _SCALE
    plot_y0 = header_h + 16 * _SCALE
    plot_x1 = w - 24 * _SCALE
    plot_y1 = h - footer_h - kpi_strip_h - 16 * _SCALE
    plot_w = max(1, plot_x1 - plot_x0)
    plot_h = max(1, plot_y1 - plot_y0)

    draw.rounded_rectangle(
        [plot_x0, plot_y0, plot_x1, plot_y1],
        radius=8 * _SCALE,
        fill=_BG_PANEL,
        outline=_BORDER,
        width=2,
    )

    all_pts = [
        p
        for entry in tracks
        for p in entry.elevation_profile
        if math.isfinite(p.dist_m) and math.isfinite(p.elev_m)
    ]

    if len(all_pts) >= 2:
        max_dist = max(1.0, max(p.dist_m for p in all_pts))
        raw_min_e = min(p.elev_m for p in all_pts)
        raw_max_e = max(p.elev_m for p in all_pts)
        span_e = max(40.0, raw_max_e - raw_min_e)
        elev_lo = max(0.0, math.floor((raw_min_e - span_e * 0.14) / 20.0) * 20.0)
        elev_hi = math.ceil((raw_max_e + span_e * 0.16) / 20.0) * 20.0
        if elev_hi <= elev_lo + 10.0:
            elev_hi = elev_lo + 50.0

        def to_px(dist_m: float, elev_m: float) -> tuple[float, float]:
            x = plot_x0 + (max(0.0, min(max_dist, dist_m)) / max_dist) * plot_w
            y = plot_y1 - ((elev_m - elev_lo) / (elev_hi - elev_lo)) * plot_h
            return (x, y)

        if elev_lo < snowline_m < elev_hi:
            _, sy = to_px(0.0, float(snowline_m))
            _draw_dashed_line(
                draw, (plot_x0 + 2, sy), (plot_x1 - 2, sy), fill=_SNOWLINE_COLOR, width=2
            )

        y_step = _nice_step(elev_hi - elev_lo, target_ticks=4)
        y_tick = math.ceil(elev_lo / y_step) * y_step
        while y_tick <= elev_hi + 1e-6:
            _, gy = to_px(0.0, y_tick)
            if plot_y0 + 4 <= gy <= plot_y1 - 4:
                draw.line([(plot_x0 + 2, gy), (plot_x1 - 2, gy)], fill=_GRID_LINE, width=2)
            lbl = f"{int(round(y_tick))} m"
            lb = draw.textbbox((0, 0), lbl, font=font_small)
            draw.text(
                (plot_x0 - (lb[2] - lb[0]) - 8 * _SCALE, gy - (lb[3] - lb[1]) / 2 - lb[1]),
                lbl,
                fill=_TEXT_MUTED,
                font=font_small,
            )
            y_tick += y_step

        max_km = max_dist / 1000.0
        x_step_km = _nice_step(max_km, target_ticks=5)
        x_km = 0.0
        while x_km <= max_km + 1e-6:
            gx, _ = to_px(x_km * 1000.0, elev_lo)
            lbl = f"{x_km:.1f} km" if x_step_km < 1.0 else f"{x_km:.0f} km"
            lb = draw.textbbox((0, 0), lbl, font=font_small)
            lx = max(plot_x0, min(plot_x1 - (lb[2] - lb[0]), gx - (lb[2] - lb[0]) / 2))
            draw.text((lx, plot_y1 + 4 * _SCALE), lbl, fill=_TEXT_MUTED, font=font_small)
            x_km += x_step_km

        for idx, entry in enumerate(tracks[:4]):
            color_hex = entry.color_hex or COMPARE_COLORS[idx % len(COMPARE_COLORS)]
            pts = [
                to_px(p.dist_m, p.elev_m)
                for p in entry.elevation_profile
                if math.isfinite(p.dist_m) and math.isfinite(p.elev_m)
            ]
            if len(pts) >= 2:
                draw.line(pts, fill=hex_to_rgba(color_hex, 255), width=6, joint="curve")
    else:
        msg = "No elevation samples available to compare"
        mb = draw.textbbox((0, 0), msg, font=font_sub)
        draw.text(
            (plot_x0 + (plot_w - (mb[2] - mb[0])) / 2, plot_y0 + (plot_h - (mb[3] - mb[1])) / 2),
            msg,
            fill=_TEXT_MUTED,
            font=font_sub,
        )

    # Bottom KPI summary strip (one card per track, 2..4 columns)
    shown = list(tracks[:4])
    if shown:
        gap = 12 * _SCALE
        cards_y0 = h - footer_h - kpi_strip_h + 12 * _SCALE
        cards_y1 = h - footer_h - 10 * _SCALE
        total_w = w - 36 * _SCALE
        card_w = (total_w - gap * (len(shown) - 1)) / len(shown)

        for idx, entry in enumerate(shown):
            cx0 = 18 * _SCALE + idx * (card_w + gap)
            cx1 = cx0 + card_w
            color_hex = entry.color_hex or COMPARE_COLORS[idx % len(COMPARE_COLORS)]
            col_rgba = hex_to_rgba(color_hex, 255)
            draw.rounded_rectangle(
                [cx0, cards_y0, cx1, cards_y1],
                radius=8 * _SCALE,
                fill=_BG_PANEL,
                outline=_BORDER,
                width=2,
            )
            draw.rectangle([cx0 + 2, cards_y0 + 10, cx0 + 10, cards_y1 - 10], fill=col_rgba)

            s = entry.summary
            max_chars = 24 if len(shown) >= 3 else 36
            draw.text(
                (cx0 + 20, cards_y0 + 10 * _SCALE),
                _truncate_text(s.name, max_chars),
                fill=_TEXT_PRIMARY,
                font=font_card,
            )
            grade_txt = s.grade or "Unrated"
            draw.text(
                (cx0 + 20, cards_y0 + 34 * _SCALE),
                f"{grade_txt} · {s.total_length_m / 1000.0:.1f} km · +{s.elev_gain_m:.0f}m",
                fill=grade_rgba(s.grade, 255),
                font=font_small,
            )
            slope_txt = f"{s.max_slope_deg:.0f}°" if s.max_slope_deg is not None else "n/a"
            status_txt = s.closure_status.upper() if s.closure_status != "open" else "OPEN"
            draw.text(
                (cx0 + 20, cards_y0 + 56 * _SCALE),
                f"Slope {slope_txt} · {s.waterway_crossing_count} xings · {status_txt}",
                fill=_TEXT_MUTED,
                font=font_small,
            )

    _draw_attribution_footer(draw, w, h, footer_text)
    return _encode_png_under_budget(img, COMPARISON_PNG_SIZE)
