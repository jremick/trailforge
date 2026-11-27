import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_AUTH_CONFIG,
  buildOfflinePackTileCachePath,
  canManageAdminCompute,
  classifyInviteStatus,
  computeStorageQuotaSummary,
  formatFriendlyApiError,
  formatStorageBytes,
  getApiBaseUrl,
  getRegistrationUiState,
  isAbsoluteHttpUrl,
  isAdminUser,
  normalizeAuthConfig,
  normalizeRouteBreakdown,
  normalizeTrafficWindow,
  resolveApiUrl,
  resolvePackAssetUrl,
  resolvePackBundleUrl,
} from "./adminHelpers";
import { ApiError, fetchAuthConfig, registerAccount } from "./api";
import { toPmtilesUrl } from "./style";

describe("Cloudflare R2 PMTiles, pack assets & VITE_API_BASE_URL resolution", () => {
  it("keeps absolute https:// R2 PMTiles URLs intact without prepending window origin", () => {
    expect(
      isAbsoluteHttpUrl("https://tiles.example.com/trailforge_vector.pmtiles")
    ).toBe(true);
    expect(isAbsoluteHttpUrl("/api/tiles/trailforge_vector.pmtiles")).toBe(
      false
    );

    expect(
      toPmtilesUrl(
        "https://tiles.example.com/trailforge_vector.pmtiles",
        "https://trailforge.example.com"
      )
    ).toBe("pmtiles://https://tiles.example.com/trailforge_vector.pmtiles");

    expect(
      toPmtilesUrl(
        "/api/tiles/trailforge_vector.pmtiles",
        "https://trailforge.example.com"
      )
    ).toBe(
      "pmtiles://https://trailforge.example.com/api/tiles/trailforge_vector.pmtiles"
    );
  });

  it("resolves API URLs cleanly with optional VITE_API_BASE_URL and leaves absolute URLs untouched", () => {
    expect(getApiBaseUrl("")).toBe("");
    expect(getApiBaseUrl("https://api.example.com///")).toBe(
      "https://api.example.com"
    );
    expect(resolveApiUrl("/api/auth/config", "")).toBe("/api/auth/config");
    expect(resolveApiUrl("/api/auth/config", "https://api.example.com/")).toBe(
      "https://api.example.com/api/auth/config"
    );
    expect(resolveApiUrl("api/health", "https://api.example.com")).toBe(
      "https://api.example.com/api/health"
    );
    expect(
      resolveApiUrl(
        "https://packs.example.com/australian-alps/manifest.json",
        "https://api.example.com"
      )
    ).toBe("https://packs.example.com/australian-alps/manifest.json");
  });

  it("preserves Cloudflare R2 absolute pack asset & bundle URLs alongside relative fallbacks", () => {
    expect(
      resolvePackAssetUrl(
        "https://packs.example.com/australian-alps/vector.pmtiles",
        "/api/packs/australian-alps/tiles/vector.pmtiles",
        ""
      )
    ).toBe("https://packs.example.com/australian-alps/vector.pmtiles");

    expect(
      resolvePackAssetUrl(
        "/api/packs/australian-alps/tiles/vector.pmtiles",
        "/api/packs/australian-alps/tiles/vector.pmtiles",
        "https://api.example.com"
      )
    ).toBe(
      "https://api.example.com/api/packs/australian-alps/tiles/vector.pmtiles"
    );

    expect(
      resolvePackBundleUrl({
        region_id: "australian-alps",
        bundle_url: "https://packs.example.com/australian-alps/bundle.tar.gz",
      })
    ).toBe("https://packs.example.com/australian-alps/bundle.tar.gz");

    expect(
      resolvePackBundleUrl(
        { region_id: "blue-mountains", bundle_url: null },
        ""
      )
    ).toBe("/api/packs/blue-mountains/bundle.tar.gz");

    expect(buildOfflinePackTileCachePath("australian-alps", "vector")).toBe(
      "/api/packs/australian-alps/tiles/vector.pmtiles"
    );
    expect(buildOfflinePackTileCachePath("australian-alps", "terrain")).toBe(
      "/api/packs/australian-alps/tiles/terrain.pmtiles"
    );

    // Protocol-relative URLs (//evil.example/...) must be rejected and fall back safely
    expect(
      resolvePackAssetUrl(
        "//evil.example/payload.pmtiles",
        "/api/packs/australian-alps/tiles/vector.pmtiles",
        ""
      )
    ).toBe("/api/packs/australian-alps/tiles/vector.pmtiles");
  });
});

describe("Registration mode UI helpers & auth config normalization", () => {
  it("handles open, invite_only, closed, and unknown registration modes", () => {
    const open = getRegistrationUiState("open");
    expect(open.canRegister).toBe(true);
    expect(open.showInviteField).toBe(false);
    expect(open.requireInviteCode).toBe(false);
    expect(open.closedNoticeText).toBeNull();

    const inviteOnly = getRegistrationUiState("invite_only");
    expect(inviteOnly.canRegister).toBe(true);
    expect(inviteOnly.showInviteField).toBe(true);
    expect(inviteOnly.requireInviteCode).toBe(true);
    expect(inviteOnly.invitePlaceholder).toBe("TF-XXXX-XXXX");
    expect(inviteOnly.inviteHelperText).toContain("Browsing maps");

    const closed = getRegistrationUiState("closed");
    expect(closed.canRegister).toBe(false);
    expect(closed.showInviteField).toBe(false);
    expect(closed.closedNoticeText).toContain("public browsing remains open");

    expect(getRegistrationUiState("unexpected_value").mode).toBe("open");
  });

  it("normalizes /api/auth/config safely", () => {
    expect(normalizeAuthConfig(null)).toEqual(DEFAULT_AUTH_CONFIG);
    expect(
      normalizeAuthConfig({
        registration_mode: "invite_only",
        demo_banner: "  Public Demo — VIC & NSW Tracks  ",
        allow_anonymous_browsing: true,
      })
    ).toEqual({
      registration_mode: "invite_only",
      demo_banner: "Public Demo — VIC & NSW Tracks",
      allow_anonymous_browsing: true,
    });
  });
});

describe("Invite status badge classification", () => {
  const now = Date.parse("2026-11-26T12:00:00Z");

  it("classifies active, exhausted, expired, and revoked invites with correct precedence", () => {
    expect(
      classifyInviteStatus(
        {
          max_uses: 5,
          use_count: 2,
          expires_at: "2026-12-26T12:00:00Z",
          revoked_at: null,
        },
        now
      )
    ).toMatchObject({
      status: "active",
      label: "Active",
      tone: "emerald",
      canRevoke: true,
      remainingUses: 3,
    });

    expect(
      classifyInviteStatus(
        {
          max_uses: 3,
          use_count: 3,
          expires_at: "2026-12-26T12:00:00Z",
          revoked_at: null,
        },
        now
      )
    ).toMatchObject({
      status: "exhausted",
      label: "Exhausted",
      tone: "amber",
      remainingUses: 0,
    });

    expect(
      classifyInviteStatus(
        {
          max_uses: 5,
          use_count: 0,
          expires_at: "2026-11-25T12:00:00Z",
          revoked_at: null,
        },
        now
      )
    ).toMatchObject({
      status: "expired",
      label: "Expired",
      tone: "muted",
      remainingUses: 0,
    });

    // Revoked takes precedence even if also expired and exhausted
    expect(
      classifyInviteStatus(
        {
          max_uses: 1,
          use_count: 1,
          expires_at: "2026-11-20T00:00:00Z",
          revoked_at: "2026-11-21T00:00:00Z",
        },
        now
      )
    ).toMatchObject({
      status: "revoked",
      label: "Revoked",
      tone: "red",
      canRevoke: false,
    });
  });
});

describe("Storage quota bar formatting & traffic normalization", () => {
  it("computes storage quota percentages, clamping, and severity tones", () => {
    const ok = computeStorageQuotaSummary(
      100 * 1024 * 1024,
      500 * 1024 * 1024
    );
    expect(ok.pct).toBe(20);
    expect(ok.clampedPct).toBe(20);
    expect(ok.tone).toBe("emerald");
    expect(ok.usedLabel).toBe("100.0 MB");
    expect(ok.quotaLabel).toBe("500.0 MB");
    expect(ok.pctLabel).toBe("20.0%");

    const warn = computeStorageQuotaSummary(
      380 * 1024 * 1024,
      500 * 1024 * 1024
    );
    expect(warn.tone).toBe("amber");

    const crit = computeStorageQuotaSummary(
      600 * 1024 * 1024,
      500 * 1024 * 1024
    );
    expect(crit.tone).toBe("red");
    expect(crit.pct).toBe(120);
    expect(crit.clampedPct).toBe(100);

    const noQuota = computeStorageQuotaSummary(1024, 0);
    expect(noQuota.quotaLabel).toBe("No quota");
    expect(noQuota.pctLabel).toBe("—");
    expect(formatStorageBytes(2 * 1024 * 1024 * 1024)).toBe("2.00 GB");
  });

  it("normalizes traffic windows and sorts route groups by request volume", () => {
    expect(
      normalizeTrafficWindow({
        request_count: 1420,
        error_count: 3,
        rate_limited_count: 12,
        bytes_out: 409600,
      })
    ).toEqual({
      requests: 1420,
      errors: 3,
      rateLimited: 12,
      bytesOut: 409600,
    });

    const sorted = normalizeRouteBreakdown([
      { route_group: "auth", request_count: 25, error_count: 2, rate_limited_count: 4 },
      { route_group: "tracks", request_count: 910, error_count: 0, rate_limited_count: 0 },
      { route_group: "tiles", request_count: 340, error_count: 1, rate_limited_count: 0 },
    ]);
    expect(sorted.map((r) => r.routeGroup)).toEqual(["tracks", "tiles", "auth"]);
    expect(sorted[2]?.rateLimited).toBe(4);
  });
});

describe("Admin-only compute gating & friendly API errors", () => {
  it("shows Build/Rebuild & Admin controls strictly to active admins", () => {
    expect(isAdminUser(null)).toBe(false);
    expect(canManageAdminCompute(null)).toBe(false);
    expect(canManageAdminCompute({ role: "member", is_active: true })).toBe(
      false
    );
    expect(canManageAdminCompute({ role: "admin", is_active: false })).toBe(
      false
    );
    expect(canManageAdminCompute({ role: "admin", is_active: true })).toBe(true);
  });

  it("formats 429 rate-limit, 403 suspended/invite-required, and 507 quota errors cleanly", () => {
    expect(
      formatFriendlyApiError(new ApiError("Rate limit exceeded", 429))
    ).toContain("Too many requests");
    expect(
      formatFriendlyApiError(new ApiError("Account is suspended", 403))
    ).toContain("Account suspended");
    expect(
      formatFriendlyApiError(new ApiError("Invalid or expired invite code", 403))
    ).toContain("Invite code required");
    expect(
      formatFriendlyApiError(new ApiError("Registration is closed", 403))
    ).toContain("closed");
    expect(
      formatFriendlyApiError(new ApiError("Insufficient storage", 507))
    ).toContain("storage quota");
  });
});

describe("Auth config & invite registration API client (mocked offline)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("falls back gracefully when /api/auth/config returns 404 or network fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("Not Found", { status: 404 }))
    );
    expect(await fetchAuthConfig()).toEqual(DEFAULT_AUTH_CONFIG);

    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new Error("Network unreachable"))
    );
    expect(await fetchAuthConfig()).toEqual(DEFAULT_AUTH_CONFIG);
  });

  it("sends invite_code in POST /api/auth/register payload", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          access_token: "tok-123",
          user: {
            id: "u-1",
            email: "mate@example.com",
            display_name: "Trail Mate",
            role: "member",
            is_active: true,
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    );
    vi.stubGlobal("fetch", fetchMock);

    const res = await registerAccount({
      email: "mate@example.com",
      display_name: "Trail Mate",
      password: "Password123!",
      invite_code: " TF-ABCD-EFGH ",
    });

    expect(res.user.role).toBe("member");
    expect(res.user.is_active).toBe(true);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      email: "mate@example.com",
      display_name: "Trail Mate",
      password: "Password123!",
      invite_code: "TF-ABCD-EFGH",
    });
  });
});
