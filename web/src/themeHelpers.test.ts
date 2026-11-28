import { describe, expect, it, vi } from "vitest";
import {
  THEME_META_COLOR,
  UI_THEME_STORAGE_KEY,
  getNextUiTheme,
  getThemeLabel,
  loadStoredUiTheme,
  normalizeUiTheme,
  resolveInitialUiTheme,
  saveStoredUiTheme,
} from "./themeHelpers";
import type { StorageLike } from "./sidebarHelpers";

function createMemoryStorage(initial?: Record<string, string>): StorageLike & {
  store: Map<string, string>;
} {
  const store = new Map<string, string>(Object.entries(initial ?? {}));
  return {
    store,
    getItem: vi.fn((key: string) => store.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => {
      store.set(key, value);
    }),
  };
}

describe("themeHelpers — Alpine Dark & Daylight Topo theme persistence", () => {
  it("normalizes valid dark/daylight/light strings and rejects invalid values", () => {
    expect(normalizeUiTheme("daylight")).toBe("daylight");
    expect(normalizeUiTheme("light")).toBe("daylight");
    expect(normalizeUiTheme("DARK")).toBe("dark");
    expect(normalizeUiTheme("neon")).toBeNull();
    expect(normalizeUiTheme(null)).toBeNull();
  });

  it("defaults to dark and persists Daylight Topo switches safely", () => {
    const storage = createMemoryStorage();
    expect(loadStoredUiTheme(storage)).toBeNull();
    expect(resolveInitialUiTheme(storage)).toBe("dark");

    const next = getNextUiTheme("dark");
    expect(next).toBe("daylight");
    expect(getThemeLabel(next)).toBe("Daylight Topo");
    expect(THEME_META_COLOR.daylight).toBe("#F4F6F3");

    saveStoredUiTheme(next, storage);
    expect(storage.store.get(UI_THEME_STORAGE_KEY)).toBe("daylight");
    expect(resolveInitialUiTheme(storage)).toBe("daylight");
    expect(getNextUiTheme("daylight")).toBe("dark");
    expect(getThemeLabel("dark")).toBe("Alpine Dark");
  });
});
