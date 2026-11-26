export const SIDEBAR_DEFAULT_WIDTH_PX = 272;
export const SIDEBAR_MIN_WIDTH_PX = 240;
export const SIDEBAR_MAX_WIDTH_PX = 520;
export const SIDEBAR_MOBILE_BREAKPOINT_PX = 768;
export const SIDEBAR_RESIZE_DISABLED_BREAKPOINT_PX = 560;
export const SIDEBAR_KEYBOARD_STEP_PX = 16;

export const SIDEBAR_WIDTH_STORAGE_KEY = "trailforge.sidebar_width_px";
export const SIDEBAR_COLLAPSED_STORAGE_KEY = "trailforge.sidebar_collapsed";

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function getDefaultStorage(): StorageLike | null {
  try {
    if (typeof window !== "undefined" && window.localStorage) {
      return window.localStorage;
    }
  } catch {
    // Ignore restricted/private-mode storage errors
  }
  return null;
}

/**
 * Compute the effective maximum sidebar width for a given viewport width so
 * the sidebar can never crowd out the map or right-hand drawers.
 */
export function getSidebarMaxWidth(viewportWidth?: number): number {
  if (
    typeof viewportWidth === "number" &&
    Number.isFinite(viewportWidth) &&
    viewportWidth > 0
  ) {
    const viewportCap = Math.floor(viewportWidth * 0.55);
    return Math.min(
      SIDEBAR_MAX_WIDTH_PX,
      Math.max(SIDEBAR_MIN_WIDTH_PX, viewportCap)
    );
  }
  return SIDEBAR_MAX_WIDTH_PX;
}

/**
 * Clamp a requested sidebar width (in px) to the supported [min, max] bounds.
 * Non-finite or non-positive inputs safely fall back to SIDEBAR_DEFAULT_WIDTH_PX.
 */
export function clampSidebarWidth(
  widthPx: number,
  viewportWidth?: number
): number {
  const maxWidth = getSidebarMaxWidth(viewportWidth);
  if (!Number.isFinite(widthPx) || widthPx <= 0) {
    return Math.min(SIDEBAR_DEFAULT_WIDTH_PX, maxWidth);
  }
  return Math.round(
    Math.min(maxWidth, Math.max(SIDEBAR_MIN_WIDTH_PX, widthPx))
  );
}

/**
 * Whether desktop drag-resizing should be active for the current viewport.
 * On very narrow phones (`<= 560px`) the CSS drawer width (`min(88vw, 320px)`) takes over.
 */
export function isSidebarResizeEnabled(viewportWidth?: number): boolean {
  if (
    typeof viewportWidth === "number" &&
    Number.isFinite(viewportWidth) &&
    viewportWidth > 0
  ) {
    return viewportWidth > SIDEBAR_RESIZE_DISABLED_BREAKPOINT_PX;
  }
  return true;
}

export function loadStoredSidebarWidth(
  storage: StorageLike | null = getDefaultStorage(),
  viewportWidth?: number
): number {
  if (storage) {
    try {
      const raw = storage.getItem(SIDEBAR_WIDTH_STORAGE_KEY);
      if (raw !== null && raw.trim() !== "") {
        const parsed = Number(raw);
        if (Number.isFinite(parsed) && parsed > 0) {
          return clampSidebarWidth(parsed, viewportWidth);
        }
      }
    } catch {
      // Fall back to default width on storage errors
    }
  }
  return clampSidebarWidth(SIDEBAR_DEFAULT_WIDTH_PX, viewportWidth);
}

export function saveStoredSidebarWidth(
  widthPx: number,
  storage: StorageLike | null = getDefaultStorage()
): number {
  const clamped = clampSidebarWidth(widthPx);
  if (storage) {
    try {
      storage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(clamped));
    } catch {
      // Ignore storage quota/security errors
    }
  }
  return clamped;
}

/**
 * Load initial sidebar collapsed state:
 * - On narrow mobile screens (`<= 768px`), always start collapsed so the map is unobstructed.
 * - On desktop screens, honor the user's saved collapsed preference (defaulting to expanded).
 */
export function loadStoredSidebarCollapsed(
  storage: StorageLike | null = getDefaultStorage(),
  viewportWidth?: number
): boolean {
  if (
    typeof viewportWidth === "number" &&
    Number.isFinite(viewportWidth) &&
    viewportWidth > 0 &&
    viewportWidth <= SIDEBAR_MOBILE_BREAKPOINT_PX
  ) {
    return true;
  }
  if (storage) {
    try {
      const raw = storage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY);
      if (raw === "true" || raw === "1") return true;
      if (raw === "false" || raw === "0") return false;
    } catch {
      // Fall back to expanded on desktop
    }
  }
  return false;
}

export function saveStoredSidebarCollapsed(
  collapsed: boolean,
  storage: StorageLike | null = getDefaultStorage()
): void {
  if (storage) {
    try {
      storage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, collapsed ? "true" : "false");
    } catch {
      // Ignore storage quota/security errors
    }
  }
}

/**
 * Compute MapLibre left camera padding in px accounting for whether the
 * sidebar is currently collapsed and its active rendered width.
 */
export function getSidebarMapLeftPadding(
  collapsed: boolean,
  widthPx: number,
  gutterPx = 24
): number {
  if (collapsed) {
    return Math.max(12, Math.round(gutterPx));
  }
  const effectiveWidth = clampSidebarWidth(widthPx);
  return effectiveWidth + Math.max(16, Math.round(gutterPx)) + 44;
}
