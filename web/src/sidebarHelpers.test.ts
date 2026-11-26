import { describe, expect, it, vi } from "vitest";
import {
  SIDEBAR_COLLAPSED_STORAGE_KEY,
  SIDEBAR_DEFAULT_WIDTH_PX,
  SIDEBAR_MAX_WIDTH_PX,
  SIDEBAR_MIN_WIDTH_PX,
  SIDEBAR_WIDTH_STORAGE_KEY,
  clampSidebarWidth,
  getSidebarMapLeftPadding,
  getSidebarMaxWidth,
  isSidebarResizeEnabled,
  loadStoredSidebarCollapsed,
  loadStoredSidebarWidth,
  saveStoredSidebarCollapsed,
  saveStoredSidebarWidth,
  type StorageLike,
} from "./sidebarHelpers";

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

describe("sidebarHelpers — width clamping & persistence", () => {
  it("enforces minimum and maximum sidebar width bounds", () => {
    expect(clampSidebarWidth(120, 1440)).toBe(SIDEBAR_MIN_WIDTH_PX);
    expect(clampSidebarWidth(SIDEBAR_MIN_WIDTH_PX, 1440)).toBe(
      SIDEBAR_MIN_WIDTH_PX
    );
    expect(clampSidebarWidth(340.6, 1440)).toBe(341);
    expect(clampSidebarWidth(900, 1440)).toBe(SIDEBAR_MAX_WIDTH_PX);
    expect(clampSidebarWidth(Number.NaN, 1440)).toBe(SIDEBAR_DEFAULT_WIDTH_PX);
    expect(clampSidebarWidth(-50, 1440)).toBe(SIDEBAR_DEFAULT_WIDTH_PX);
  });

  it("caps effective maximum width dynamically on smaller desktop viewports", () => {
    expect(getSidebarMaxWidth(700)).toBe(385);
    expect(clampSidebarWidth(480, 700)).toBe(385);
    expect(getSidebarMaxWidth(400)).toBe(SIDEBAR_MIN_WIDTH_PX);
    expect(isSidebarResizeEnabled(1280)).toBe(true);
    expect(isSidebarResizeEnabled(560)).toBe(false);
    expect(isSidebarResizeEnabled(390)).toBe(false);
  });

  it("remembers user-chosen sidebar width and clamps corrupted values safely", () => {
    const storage = createMemoryStorage();
    expect(loadStoredSidebarWidth(storage, 1440)).toBe(
      SIDEBAR_DEFAULT_WIDTH_PX
    );

    const saved = saveStoredSidebarWidth(380, storage);
    expect(saved).toBe(380);
    expect(storage.store.get(SIDEBAR_WIDTH_STORAGE_KEY)).toBe("380");
    expect(loadStoredSidebarWidth(storage, 1440)).toBe(380);

    // Below minimum gets clamped up on save
    expect(saveStoredSidebarWidth(100, storage)).toBe(SIDEBAR_MIN_WIDTH_PX);
    expect(loadStoredSidebarWidth(storage, 1440)).toBe(SIDEBAR_MIN_WIDTH_PX);

    // Corrupted stored value falls back cleanly to default
    const badStorage = createMemoryStorage({
      [SIDEBAR_WIDTH_STORAGE_KEY]: "not-a-number",
    });
    expect(loadStoredSidebarWidth(badStorage, 1440)).toBe(
      SIDEBAR_DEFAULT_WIDTH_PX
    );

    // Temporary narrow viewport clamps rendered width without losing support
    const wideStorage = createMemoryStorage({
      [SIDEBAR_WIDTH_STORAGE_KEY]: "460",
    });
    expect(loadStoredSidebarWidth(wideStorage, 720)).toBe(396);
    expect(loadStoredSidebarWidth(wideStorage, 1440)).toBe(460);
  });
});

describe("sidebarHelpers — collapsed state & map padding", () => {
  it("persists collapsed preference on desktop and starts collapsed on narrow mobile", () => {
    const storage = createMemoryStorage();
    expect(loadStoredSidebarCollapsed(storage, 1280)).toBe(false);
    expect(loadStoredSidebarCollapsed(storage, 768)).toBe(true);
    expect(loadStoredSidebarCollapsed(storage, 390)).toBe(true);

    saveStoredSidebarCollapsed(true, storage);
    expect(storage.store.get(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe("true");
    expect(loadStoredSidebarCollapsed(storage, 1280)).toBe(true);

    saveStoredSidebarCollapsed(false, storage);
    expect(storage.store.get(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe("false");
    expect(loadStoredSidebarCollapsed(storage, 1280)).toBe(false);
    // Narrow mobile still opens collapsed so map isn't obscured on launch
    expect(loadStoredSidebarCollapsed(storage, 390)).toBe(true);
  });

  it("computes dynamic map left padding from collapsed state and active width", () => {
    expect(getSidebarMapLeftPadding(true, 380, 16)).toBe(16);
    expect(getSidebarMapLeftPadding(true, 380, 24)).toBe(24);
    expect(getSidebarMapLeftPadding(false, 272, 24)).toBe(272 + 24 + 44);
    expect(getSidebarMapLeftPadding(false, 420, 24)).toBe(420 + 24 + 44);
  });
});
