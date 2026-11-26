import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AUTH_TOKEN_STORAGE_KEY,
  clearLegacyStoredAuthToken,
  fetchAuthMe,
  getAccessToken,
  getStoredAuthToken,
  loginAccount,
  logoutAccount,
  registerAccount,
  setAccessToken,
  setStoredAuthToken,
  shouldUseBearerStorage,
} from "./api";
import { sanitizeReportPhotoUrl } from "./components/inspector";
import { isNativeApp } from "./native/bridge";

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

describe("M-4: Cookie-only same-origin web auth & Bearer storage gating", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns false from shouldUseBearerStorage() on same-origin browser web and true only when cross-origin or native Capacitor", () => {
    expect(isNativeApp({ windowObj: {} })).toBe(false);
    expect(
      isNativeApp({
        windowObj: {
          Capacitor: { isNativePlatform: () => false, getPlatform: () => "web" },
        },
      })
    ).toBe(false);
    expect(
      isNativeApp({
        windowObj: {
          Capacitor: { isNativePlatform: () => true, getPlatform: () => "ios" },
        },
      })
    ).toBe(true);
    expect(
      isNativeApp({
        windowObj: {
          Capacitor: {
            isNativePlatform: () => true,
            getPlatform: () => "android",
          },
        },
      })
    ).toBe(true);

    // Same-origin browser web: cookie-only, NEVER localStorage Bearer
    expect(shouldUseBearerStorage({ apiBaseUrl: "", windowObj: {} })).toBe(
      false
    );
    expect(shouldUseBearerStorage({ apiBaseUrl: "   ", windowObj: {} })).toBe(
      false
    );

    // Cross-origin API (VITE_API_BASE_URL set)
    expect(
      shouldUseBearerStorage({
        apiBaseUrl: "https://api.example.com",
        windowObj: {},
      })
    ).toBe(true);

    // Native Capacitor container (even without explicit apiBaseUrl)
    expect(
      shouldUseBearerStorage({
        apiBaseUrl: "",
        windowObj: {
          Capacitor: { isNativePlatform: () => true, getPlatform: () => "ios" },
        },
      })
    ).toBe(true);
    expect(
      shouldUseBearerStorage({
        apiBaseUrl: "",
        windowObj: {
          Capacitor: {
            isNativePlatform: () => true,
            getPlatform: () => "android",
          },
        },
      })
    ).toBe(true);
  });

  it("never writes or reads localStorage on same-origin web and purges any legacy token on boot & logout", async () => {
    const mockStorage = createMockLocalStorage({
      [AUTH_TOKEN_STORAGE_KEY]: "legacy-v1-token",
    });
    vi.stubGlobal("window", { localStorage: mockStorage });

    // Default Node/browser env without VITE_API_BASE_URL or Capacitor is same-origin web
    expect(shouldUseBearerStorage()).toBe(false);

    // Never reads localStorage even if a legacy key was present
    expect(getStoredAuthToken()).toBeNull();
    expect(getAccessToken()).toBeNull();
    expect(mockStorage.getItem).not.toHaveBeenCalled();

    // Boot cleanup purges legacy token
    clearLegacyStoredAuthToken();
    expect(mockStorage.store.has(AUTH_TOKEN_STORAGE_KEY)).toBe(false);

    // setAccessToken / setStoredAuthToken writes ZERO keys on same-origin web
    setAccessToken("should-not-persist");
    setStoredAuthToken("also-should-not-persist");
    expect(mockStorage.setItem).not.toHaveBeenCalled();
    expect(mockStorage.store.size).toBe(0);

    // Login, register, fetchAuthMe, and logout use credentials: "include" with NO Authorization header
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            access_token: "server-token-login",
            user: {
              id: "u-1",
              email: "driver@example.com",
              display_name: "Driver",
              role: "member",
              is_active: true,
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        )
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            access_token: "server-token-register",
            user: {
              id: "u-2",
              email: "new@example.com",
              display_name: "New Driver",
              role: "member",
              is_active: true,
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        )
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ authenticated: true, user: null }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    vi.stubGlobal("fetch", fetchMock);

    await loginAccount({ email: "driver@example.com", password: "Password123!" });
    await registerAccount({
      email: "new@example.com",
      display_name: "New Driver",
      password: "Password123!",
    });
    await fetchAuthMe();
    await logoutAccount();

    expect(mockStorage.setItem).not.toHaveBeenCalled();
    expect(mockStorage.store.size).toBe(0);

    for (const [, init] of fetchMock.mock.calls as [string, RequestInit][]) {
      expect(init.credentials).toBe("include");
      const headers = (init.headers ?? {}) as Record<string, string>;
      expect(headers.Authorization).toBeUndefined();
    }
  });

  it("persists and clears Bearer token in localStorage when cross-origin or running in native Capacitor", () => {
    const mockStorage = createMockLocalStorage();
    vi.stubGlobal("window", {
      localStorage: mockStorage,
      Capacitor: { isNativePlatform: () => true, getPlatform: () => "ios" },
    });

    expect(shouldUseBearerStorage()).toBe(true);

    setAccessToken("  native-jwt-token  ");
    expect(mockStorage.setItem).toHaveBeenCalledWith(
      AUTH_TOKEN_STORAGE_KEY,
      "native-jwt-token"
    );
    expect(getAccessToken()).toBe("native-jwt-token");

    // Boot cleanup must NOT wipe token inside native Capacitor
    clearLegacyStoredAuthToken();
    expect(getAccessToken()).toBe("native-jwt-token");

    // Explicit clear removes the token
    setAccessToken(null);
    expect(mockStorage.store.has(AUTH_TOKEN_STORAGE_KEY)).toBe(false);
    expect(getAccessToken()).toBeNull();

    // Cross-origin API_BASE_URL in non-native browser also permits Bearer storage
    vi.stubGlobal("window", { localStorage: mockStorage });
    const crossOriginCtx = { apiBaseUrl: "https://api.example.com" };
    expect(shouldUseBearerStorage(crossOriginCtx)).toBe(true);
    setAccessToken("cross-origin-token", crossOriginCtx);
    expect(getAccessToken(crossOriginCtx)).toBe("cross-origin-token");
    setAccessToken(null, crossOriginCtx);
    expect(getAccessToken(crossOriginCtx)).toBeNull();
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
