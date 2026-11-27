import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AUTH_TOKEN_STORAGE_KEY,
  clearLegacyStoredAuthToken,
  fetchAuthMe,
  fetchReportsGeoJson,
  loginAccount,
  logoutAccount,
  registerAccount,
  uploadMobileTripSync,
  usesSessionBearer,
} from "./api";
import { sanitizeReportPhotoUrl } from "./components/inspector";

interface MockStorage {
  store: Map<string, string>;
  getItem: ReturnType<typeof vi.fn<(key: string) => string | null>>;
  setItem: ReturnType<typeof vi.fn<(key: string, value: string) => void>>;
  removeItem: ReturnType<typeof vi.fn<(key: string) => void>>;
  clear: ReturnType<typeof vi.fn<() => void>>;
  readonly length: number;
}

function createMockLocalStorage(
  initial?: Record<string, string>
): MockStorage {
  const store = new Map<string, string>(Object.entries(initial ?? {}));
  return {
    store,
    getItem: vi.fn((key: string) => store.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => {
      store.set(key, String(value));
    }),
    removeItem: vi.fn((key: string) => {
      store.delete(key);
    }),
    clear: vi.fn(() => {
      store.clear();
    }),
    get length() {
      return store.size;
    },
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const loginInput = { email: "driver@example.com", password: "test-password" };
const loginPayload = {
  access_token: "fixture-session-token",
  user: { id: "u-1", email: loginInput.email, display_name: "Driver", role: "member", is_active: true },
};

describe("Browser session authentication", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("uses the HttpOnly cookie for a same-origin API including an explicit same-origin base URL", async () => {
    const storage = createMockLocalStorage({ [AUTH_TOKEN_STORAGE_KEY]: "old-token" });
    vi.stubEnv("VITE_API_BASE_URL", "https://trails.example.test");
    vi.stubGlobal("window", {
      location: new URL("https://trails.example.test/"),
      localStorage: storage,
    });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(loginPayload))
      .mockResolvedValueOnce(jsonResponse(loginPayload, 201))
      .mockResolvedValueOnce(jsonResponse({ authenticated: true, user: loginPayload.user }))
      .mockResolvedValueOnce(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    expect(usesSessionBearer()).toBe(false);
    clearLegacyStoredAuthToken();
    await loginAccount(loginInput);
    await registerAccount({ ...loginInput, display_name: "Driver" });
    await fetchAuthMe();
    await logoutAccount();

    expect(storage.store.has(AUTH_TOKEN_STORAGE_KEY)).toBe(false);
    expect(storage.getItem).not.toHaveBeenCalledWith(AUTH_TOKEN_STORAGE_KEY);
    expect(storage.setItem).not.toHaveBeenCalled();
    for (const [, init] of fetchMock.mock.calls as [string, RequestInit][]) {
      expect(init.credentials).toBe("include");
      expect(new Headers(init.headers).has("Authorization")).toBe(false);
    }
  });

  it.each([
    { platform: "cross-origin", base: "https://api.example.test" },
    { platform: "ios", base: "https://api.example.test" },
    { platform: "android", base: "https://api.example.test" },
  ])("keeps a $platform bearer in memory and clears it on logout even if offline", async ({ platform, base }) => {
    const storage = createMockLocalStorage({ [AUTH_TOKEN_STORAGE_KEY]: "old-token" });
    const capacitor = platform === "cross-origin" ? undefined : {
      isNativePlatform: () => true,
      getPlatform: () => platform,
    };
    vi.stubEnv("VITE_API_BASE_URL", base);
    vi.stubGlobal("window", {
      location: new URL("https://trails.example.test/"),
      localStorage: storage,
      Capacitor: capacitor,
    });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(loginPayload))
      .mockResolvedValueOnce(jsonResponse({ authenticated: true, user: loginPayload.user }))
      .mockResolvedValueOnce(jsonResponse({ type: "FeatureCollection", features: [] }))
      .mockRejectedValueOnce(new TypeError("Offline"))
      .mockResolvedValueOnce(jsonResponse({ authenticated: false, user: null }));
    vi.stubGlobal("fetch", fetchMock);

    expect(usesSessionBearer()).toBe(true);
    clearLegacyStoredAuthToken();
    await loginAccount(loginInput);
    await fetchAuthMe();
    await fetchReportsGeoJson({ anonymous: true });
    await expect(logoutAccount()).rejects.toThrow("Offline");
    await fetchAuthMe();

    expect(storage.store.has(AUTH_TOKEN_STORAGE_KEY)).toBe(false);
    expect(storage.getItem).not.toHaveBeenCalledWith(AUTH_TOKEN_STORAGE_KEY);
    expect(storage.setItem).not.toHaveBeenCalled();
    const requests = fetchMock.mock.calls as [string, RequestInit][];
    expect(requests.every(([, init]) => init.credentials === "omit")).toBe(true);
    expect(requests.map(([, init]) => new Headers(init.headers).get("Authorization"))).toEqual([
      null, "Bearer fixture-session-token", null, "Bearer fixture-session-token", null,
    ]);
  });

  it("checks native runtime independently of API base configuration", () => {
    vi.stubEnv("VITE_API_BASE_URL", "");
    vi.stubGlobal("window", {
      Capacitor: { isNativePlatform: () => true, getPlatform: () => "ios" },
    });
    expect(usesSessionBearer()).toBe(true);
  });
});

describe("Account-bound API requests", () => {
  afterEach(() => vi.unstubAllGlobals());

  const payload = {
    name: "Offline drive",
    points: [{ lon: 148.1, lat: -36.5 }, { lon: 148.11, lat: -36.51 }],
    visibility: "private" as const,
    privacy_trim_m: 250,
  };

  it("preserves the originating account and cancellation signal during upload fallback", async () => {
    const signal = new AbortController().signal;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "Not found" }, 404))
      .mockResolvedValueOnce(jsonResponse({ id: "fixture-trip" }, 201));
    vi.stubGlobal("fetch", fetchMock);

    await expect(uploadMobileTripSync(payload, { ownerId: "u-1", signal })).resolves.toMatchObject({
      id: "fixture-trip",
    });
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/trips/mobile-sync", "/api/trips/upload",
    ]);
    for (const [, init] of fetchMock.mock.calls as [string, RequestInit][]) {
      expect(new Headers(init.headers).get("X-TrailForge-Account")).toBe("u-1");
      expect(init.signal).toBe(signal);
    }
  });

  it("does not start fallback upload after an account change aborts the first request", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn(async () => {
      controller.abort();
      return jsonResponse({ detail: "Not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(uploadMobileTripSync(payload, {
      ownerId: "u-1",
      signal: controller.signal,
    })).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("L-4: Condition report photo_url sanitization", () => {
  it("accepts canonical local API report photo paths", () => {
    expect(sanitizeReportPhotoUrl("/api/reports/photos/abc123.jpg")).toBe(
      "/api/reports/photos/abc123.jpg"
    );
    expect(
      sanitizeReportPhotoUrl("/api/reports/photos/report_2026-11-26.webp")
    ).toBe("/api/reports/photos/report_2026-11-26.webp");
    expect(sanitizeReportPhotoUrl("  /api/reports/photos/img-01.png  ")).toBe(
      "/api/reports/photos/img-01.png"
    );
  });

  it("rejects protocol-relative, javascript:, data:, external http(s), and path-traversal URLs", () => {
    const malicious = [
      "//evil.com/pwn.png",
      "//evil.com/api/reports/photos/pwn.png",
      "javascript:alert(1)",
      "JAVASCRIPT:alert(document.domain)",
      "data:image/svg+xml,<svg onload=alert(1)>",
      "https://evil.com/pwn.png",
      "http://evil.com/pwn.png",
      "/api/reports/photos/../secret.png",
      "/api/reports/photos/..",
      "/api/reports/photos/.",
      "/api/reports/photos/sub/file.jpg",
      "/api/reports/photos/abc.jpg?redirect=https://evil.com",
      "/api/reports/photos/abc.jpg#frag",
      "/other/path/image.png",
      "",
      null,
      undefined,
    ];

    for (const candidate of malicious) {
      expect(sanitizeReportPhotoUrl(candidate)).toBeNull();
    }
  });
});

interface NodeFsSubset {
  existsSync: (path: URL | string) => boolean;
  readFileSync: (path: URL | string, encoding: "utf8") => string;
}

async function loadNodeFs(): Promise<NodeFsSubset> {
  const specifier = "node:fs";
  return (await import(/* @vite-ignore */ specifier)) as NodeFsSubset;
}

describe("M-3: Security headers & CSP in Cloudflare Pages _headers and Nginx", () => {
  it("ships web/public/_headers with required security headers and MapLibre-compatible CSP", async () => {
    const fs = await loadNodeFs();
    const headersUrl = new URL("../public/_headers", import.meta.url);
    expect(fs.existsSync(headersUrl)).toBe(true);

    const content = fs.readFileSync(headersUrl, "utf8");
    expect(content).toContain("/*");
    expect(content).toContain("X-Content-Type-Options: nosniff");
    expect(content).toContain("X-Frame-Options: DENY");
    expect(content).toContain(
      "Referrer-Policy: strict-origin-when-cross-origin"
    );
    expect(content).toContain(
      "Permissions-Policy: camera=(), microphone=(), geolocation=(self)"
    );
    expect(content).toContain("Strict-Transport-Security:");

    const cspLine = content
      .split(/\r?\n/)
      .find((line: string) =>
        line.trim().startsWith("Content-Security-Policy:")
      );
    expect(cspLine).toBeDefined();
    expect(cspLine).toContain("default-src 'self'");
    expect(cspLine).toContain("script-src 'self'");
    expect(cspLine).toContain("style-src 'self' 'unsafe-inline'");
    expect(cspLine).toContain("worker-src 'self' blob:");
    expect(cspLine).toContain("img-src 'self' data: blob: https:");
    expect(cspLine).toContain("connect-src 'self' https: data: blob:");
    expect(cspLine).toContain("object-src 'none'");
    expect(cspLine).toContain("base-uri 'self'");
    expect(cspLine).toContain("frame-ancestors 'none'");
    expect(cspLine).not.toContain("'unsafe-eval'");
  });

  it("emits matching global security headers and CSP in web/nginx.conf across all locations", async () => {
    const fs = await loadNodeFs();
    const nginxUrl = new URL("../nginx.conf", import.meta.url);
    expect(fs.existsSync(nginxUrl)).toBe(true);

    const content = fs.readFileSync(nginxUrl, "utf8");
    expect(content).toContain('add_header X-Content-Type-Options "nosniff" always;');
    expect(content).toContain('add_header X-Frame-Options "DENY" always;');
    expect(content).toContain(
      'add_header Referrer-Policy "strict-origin-when-cross-origin" always;'
    );
    expect(content).toContain(
      'add_header Permissions-Policy "camera=(), microphone=(), geolocation=(self)" always;'
    );
    expect(content).toContain("add_header Content-Security-Policy ");
    expect(content).toContain("worker-src 'self' blob:");
    expect(content).toContain("frame-ancestors 'none'");
    expect(content).toContain("object-src 'none'");
    expect(content).toContain("base-uri 'self'");
    expect(content).toContain("server_tokens off;");
  });
});
