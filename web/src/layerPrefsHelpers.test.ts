import { describe, expect, it, vi } from "vitest";
import {
  CANONICAL_LAYER_ORDER,
  DEFAULT_LAYER_VISIBILITY,
  DEFAULT_PUBLIC_UI_CONFIG,
  USER_LAYER_PREFS_STORAGE_KEY,
  clearStoredUserLayerPrefs,
  loadStoredUserLayerPrefs,
  moveLayerInOrder,
  normalizeLayerOrder,
  normalizeLayerVisibility,
  normalizePublicUiConfig,
  reorderLayerKeys,
  resolveEffectiveLayerPreferences,
  saveStoredUserLayerPrefs,
  shouldShowStackTelemetrySection,
  type StorageWithRemove,
} from "./layerPrefsHelpers";

function createMemoryStorage(
  initial?: Record<string, string>
): StorageWithRemove & { store: Map<string, string> } {
  const store = new Map<string, string>(Object.entries(initial ?? {}));
  return {
    store,
    getItem: vi.fn((key: string) => store.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => {
      store.set(key, value);
    }),
    removeItem: vi.fn((key: string) => {
      store.delete(key);
    }),
  };
}

describe("layerPrefsHelpers — default ordering & reordering", () => {
  it("matches the requested default Map Layers sidebar order", () => {
    expect(CANONICAL_LAYER_ORDER).toEqual([
      "contoursHillshade",
      "parksWaterways",
      "pois",
      "tracks",
      "steepHighlights",
      "hotspots",
      "closures",
      "reports",
      "unmapped",
      "snowLine",
    ]);
  });

  it("normalizes partial, duplicate, or invalid layer orders safely", () => {
    const normalized = normalizeLayerOrder([
      "snowLine",
      "bogus_key",
      "tracks",
      "snowLine",
    ]);
    expect(normalized.slice(0, 2)).toEqual(["snowLine", "tracks"]);
    expect(normalized).toHaveLength(CANONICAL_LAYER_ORDER.length);
    expect(new Set(normalized).size).toBe(CANONICAL_LAYER_ORDER.length);
  });

  it("moves layers up/down and supports drag-and-drop reordering", () => {
    const movedUp = moveLayerInOrder(CANONICAL_LAYER_ORDER, "parksWaterways", "up");
    expect(movedUp.slice(0, 3)).toEqual([
      "parksWaterways",
      "contoursHillshade",
      "pois",
    ]);

    // Boundary moves are no-ops
    expect(moveLayerInOrder(CANONICAL_LAYER_ORDER, "contoursHillshade", "up")).toEqual(
      CANONICAL_LAYER_ORDER
    );
    expect(moveLayerInOrder(CANONICAL_LAYER_ORDER, "snowLine", "down")).toEqual(
      CANONICAL_LAYER_ORDER
    );

    const dragged = reorderLayerKeys(CANONICAL_LAYER_ORDER, "snowLine", "pois");
    expect(dragged.slice(0, 4)).toEqual([
      "contoursHillshade",
      "parksWaterways",
      "snowLine",
      "pois",
    ]);
  });
});

describe("layerPrefsHelpers — admin defaults vs user overrides & persistence", () => {
  it("resolves admin defaults when user has no custom prefs and layers user overrides on top", () => {
    const adminConfig = normalizePublicUiConfig({
      default_layer_order: ["tracks", "closures", "pois"],
      default_layer_visibility: {
        ...DEFAULT_LAYER_VISIBILITY,
        snowLine: true,
        hotspots: false,
      },
      show_stack_telemetry_public: true,
    });

    // No user overrides -> uses admin defaults cleanly
    const fromAdmin = resolveEffectiveLayerPreferences(adminConfig, null, 1100);
    expect(fromAdmin.hasCustomPreferences).toBe(false);
    expect(fromAdmin.layerOrder.slice(0, 3)).toEqual(["tracks", "closures", "pois"]);
    expect(fromAdmin.layerState.snowLine).toBe(true);
    expect(fromAdmin.layerState.hotspots).toBe(false);
    expect(fromAdmin.layerState.snowLineElevationM).toBe(1100);

    // User overrides order & one toggle -> preserves admin defaults for untouched keys
    const withUser = resolveEffectiveLayerPreferences(
      adminConfig,
      {
        layerOrder: ["pois", "tracks"],
        layerVisibility: { unmapped: true, snowLine: false },
      },
      1200
    );
    expect(withUser.hasCustomPreferences).toBe(true);
    expect(withUser.layerOrder.slice(0, 3)).toEqual(["pois", "tracks", "closures"]);
    expect(withUser.layerState.unmapped).toBe(true);
    expect(withUser.layerState.snowLine).toBe(false);
    expect(withUser.layerState.hotspots).toBe(false);
  });

  it("persists, loads, and clears user layer preferences in localStorage safely", () => {
    const storage = createMemoryStorage();
    expect(loadStoredUserLayerPrefs(storage)).toEqual({
      layerOrder: null,
      layerVisibility: null,
    });

    saveStoredUserLayerPrefs(
      {
        layerOrder: ["tracks", "pois"],
        layerVisibility: { snowLine: true, tracks: false },
      },
      storage
    );
    expect(storage.store.has(USER_LAYER_PREFS_STORAGE_KEY)).toBe(true);

    const loaded = loadStoredUserLayerPrefs(storage);
    expect(loaded.layerOrder?.slice(0, 2)).toEqual(["tracks", "pois"]);
    expect(loaded.layerOrder).toHaveLength(CANONICAL_LAYER_ORDER.length);
    expect(loaded.layerVisibility).toEqual({ snowLine: true, tracks: false });

    clearStoredUserLayerPrefs(storage);
    expect(storage.store.has(USER_LAYER_PREFS_STORAGE_KEY)).toBe(false);
    expect(loadStoredUserLayerPrefs(storage)).toEqual({
      layerOrder: null,
      layerVisibility: null,
    });

    // Corrupted JSON falls back cleanly
    const badStorage = createMemoryStorage({
      [USER_LAYER_PREFS_STORAGE_KEY]: "{not valid json",
    });
    expect(loadStoredUserLayerPrefs(badStorage)).toEqual({
      layerOrder: null,
      layerVisibility: null,
    });
  });

  it("normalizes partial visibility maps against defaults", () => {
    const vis = normalizeLayerVisibility({ snowLine: true, tracks: "bad" });
    expect(vis.snowLine).toBe(true);
    expect(vis.tracks).toBe(true);
    expect(DEFAULT_PUBLIC_UI_CONFIG.show_stack_telemetry_public).toBe(false);
  });
});

describe("layerPrefsHelpers — Stack & Offline Telemetry visibility rules", () => {
  it("hides telemetry section from anonymous visitors by default, shows when configured, and always shows to active members & admins", () => {
    expect(shouldShowStackTelemetrySection(null, false)).toBe(false);
    expect(shouldShowStackTelemetrySection(undefined, false)).toBe(false);
    expect(shouldShowStackTelemetrySection(null, true)).toBe(true);

    expect(
      shouldShowStackTelemetrySection({ role: "member", is_active: true }, false)
    ).toBe(true);
    expect(
      shouldShowStackTelemetrySection({ role: "admin", is_active: true }, false)
    ).toBe(true);

    // Suspended users fall back to public policy
    expect(
      shouldShowStackTelemetrySection({ role: "member", is_active: false }, false)
    ).toBe(false);
    expect(
      shouldShowStackTelemetrySection({ role: "member", is_active: false }, true)
    ).toBe(true);
  });
});
