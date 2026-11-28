import type { StorageLike } from "./sidebarHelpers";

export type UiTheme = "dark" | "daylight";

export const UI_THEME_STORAGE_KEY = "trailforge.ui_theme_v1";

export const THEME_META_COLOR: Readonly<Record<UiTheme, string>> = {
  dark: "#0B1111",
  daylight: "#F4F6F3",
};

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

export function normalizeUiTheme(raw: unknown): UiTheme | null {
  if (typeof raw !== "string") return null;
  const val = raw.trim().toLowerCase();
  if (val === "daylight" || val === "light") return "daylight";
  if (val === "dark") return "dark";
  return null;
}

export function loadStoredUiTheme(
  storage: StorageLike | null = getDefaultStorage()
): UiTheme | null {
  if (!storage) return null;
  try {
    return normalizeUiTheme(storage.getItem(UI_THEME_STORAGE_KEY));
  } catch {
    return null;
  }
}

export function saveStoredUiTheme(
  theme: UiTheme,
  storage: StorageLike | null = getDefaultStorage()
): UiTheme {
  const clean: UiTheme = theme === "daylight" ? "daylight" : "dark";
  if (storage) {
    try {
      storage.setItem(UI_THEME_STORAGE_KEY, clean);
    } catch {
      // Ignore storage quota/security errors
    }
  }
  return clean;
}

export function resolveInitialUiTheme(
  storage: StorageLike | null = getDefaultStorage(),
  fallback: UiTheme = "dark"
): UiTheme {
  return loadStoredUiTheme(storage) ?? fallback;
}

export function getNextUiTheme(current: UiTheme): UiTheme {
  return current === "dark" ? "daylight" : "dark";
}

export function getThemeLabel(theme: UiTheme): string {
  return theme === "daylight" ? "Daylight Topo" : "Alpine Dark";
}

export function applyUiThemeToDocument(
  theme: UiTheme,
  doc: Document | null = typeof document !== "undefined" ? document : null
): void {
  if (!doc) return;
  const clean: UiTheme = theme === "daylight" ? "daylight" : "dark";
  doc.documentElement.setAttribute("data-theme", clean);

  const colorScheme = doc.querySelector<HTMLMetaElement>(
    'meta[name="color-scheme"]'
  );
  if (colorScheme) {
    colorScheme.content = clean === "daylight" ? "light" : "dark";
  }

  const themeColor = doc.querySelector<HTMLMetaElement>(
    'meta[name="theme-color"]'
  );
  if (themeColor) {
    themeColor.content = THEME_META_COLOR[clean];
  }
}
