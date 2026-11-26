import type {
  AdminInviteItem,
  AdminRouteBreakdownItem,
  AdminTrafficWindow,
  AuthConfigResponse,
  AuthUser,
  OfflinePackRegionItem,
  RegistrationMode,
} from "./types";

export const DEFAULT_AUTH_CONFIG: AuthConfigResponse = {
  registration_mode: "open",
  demo_banner: "",
  allow_anonymous_browsing: true,
};

export function isAbsoluteHttpUrl(value: string | null | undefined): boolean {
  if (!value) return false;
  return /^https?:\/\//i.test(value.trim());
}

/**
 * Resolve optional `VITE_API_BASE_URL` (default `""` for same-origin nginx /
 * Cloudflare Pages Functions proxy). Strips any trailing slashes cleanly.
 */
export function getApiBaseUrl(override?: string): string {
  const raw =
    override !== undefined
      ? override
      : ((import.meta as { env?: Record<string, unknown> }).env
          ?.VITE_API_BASE_URL ?? "");
  return String(raw).trim().replace(/\/+$/, "");
}

/**
 * Resolve an API endpoint path against `VITE_API_BASE_URL` when configured,
 * preserving already-absolute `http://` / `https://` URLs untouched.
 */
export function resolveApiUrl(
  pathOrUrl: string,
  baseOverride?: string
): string {
  const trimmed = pathOrUrl.trim();
  if (isAbsoluteHttpUrl(trimmed)) return trimmed;
  const base = getApiBaseUrl(baseOverride);
  if (!base) return trimmed;
  const cleanPath = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  return `${base}${cleanPath}`;
}

/**
 * Resolve an offline pack asset URL (manifest, snapshot, bundle, or PMTiles).
 * Absolute Cloudflare R2 `https://` URLs pass straight through untouched;
 * relative `/api/packs/...` paths resolve cleanly against `VITE_API_BASE_URL`.
 */
export function resolvePackAssetUrl(
  rawUrl: string | null | undefined,
  fallbackPath: string,
  baseOverride?: string
): string {
  const trimmed = (rawUrl ?? "").trim();
  if (trimmed) {
    if (isAbsoluteHttpUrl(trimmed)) return trimmed;
    if (trimmed.startsWith("/")) return resolveApiUrl(trimmed, baseOverride);
  }
  return resolveApiUrl(fallbackPath, baseOverride);
}

/**
 * Resolve the `.tar.gz` download URL for an offline regional pack card.
 */
export function resolvePackBundleUrl(
  region: Pick<OfflinePackRegionItem, "region_id" | "bundle_url">,
  baseOverride?: string
): string {
  const fallback = `/api/packs/${encodeURIComponent(region.region_id)}/bundle.tar.gz`;
  return resolvePackAssetUrl(region.bundle_url, fallback, baseOverride);
}

/**
 * Canonical same-origin CacheStorage pathname for downloaded offline pack PMTiles
 * so `sw.js` serves HTTP 206 Range slices 100% offline even when downloaded from R2.
 */
export function buildOfflinePackTileCachePath(
  regionId: string,
  kind: "vector" | "terrain"
): string {
  return `/api/packs/${encodeURIComponent(regionId)}/tiles/${kind}.pmtiles`;
}

export function normalizeRegistrationMode(
  raw: unknown
): RegistrationMode {
  if (raw === "invite_only" || raw === "closed" || raw === "open") {
    return raw;
  }
  return "open";
}

export function normalizeAuthConfig(raw: unknown): AuthConfigResponse {
  if (!raw || typeof raw !== "object") {
    return { ...DEFAULT_AUTH_CONFIG };
  }
  const obj = raw as Record<string, unknown>;
  return {
    registration_mode: normalizeRegistrationMode(obj.registration_mode),
    demo_banner:
      typeof obj.demo_banner === "string" ? obj.demo_banner.trim() : "",
    allow_anonymous_browsing:
      typeof obj.allow_anonymous_browsing === "boolean"
        ? obj.allow_anonymous_browsing
        : true,
  };
}

export interface RegistrationUiState {
  mode: RegistrationMode;
  canRegister: boolean;
  showInviteField: boolean;
  requireInviteCode: boolean;
  invitePlaceholder: string;
  inviteHelperText: string | null;
  closedNoticeText: string | null;
  ctaLabel: string;
}

/**
 * Derive crisp registration UI state from `GET /api/auth/config` registration_mode.
 */
export function getRegistrationUiState(
  mode: RegistrationMode | string | null | undefined
): RegistrationUiState {
  const normalized = normalizeRegistrationMode(mode);
  if (normalized === "closed") {
    return {
      mode: "closed",
      canRegister: false,
      showInviteField: false,
      requireInviteCode: false,
      invitePlaceholder: "TF-XXXX-XXXX",
      inviteHelperText: null,
      closedNoticeText:
        "Invite/registration currently closed — public browsing remains open.",
      ctaLabel: "Sign In",
    };
  }
  if (normalized === "invite_only") {
    return {
      mode: "invite_only",
      canRegister: true,
      showInviteField: true,
      requireInviteCode: true,
      invitePlaceholder: "TF-XXXX-XXXX",
      inviteHelperText:
        "Browsing maps & route planning is open to everyone. Enter a friend invite code to upload trips & reports.",
      closedNoticeText: null,
      ctaLabel: "Sign In / Redeem Invite",
    };
  }
  return {
    mode: "open",
    canRegister: true,
    showInviteField: false,
    requireInviteCode: false,
    invitePlaceholder: "TF-XXXX-XXXX",
    inviteHelperText: null,
    closedNoticeText: null,
    ctaLabel: "Sign In / Create Account",
  };
}

/**
 * Check whether the current user is an active administrator.
 */
export function isAdminUser(
  user: Pick<AuthUser, "role" | "is_active"> | null | undefined
): boolean {
  if (!user) return false;
  return user.role === "admin" && user.is_active !== false;
}

/**
 * Gate heavy compute actions (Build/Rebuild regional packs, sync closures, recompute grades)
 * strictly to active admins so visitors & members never trigger HTTP 403 buttons.
 */
export function canManageAdminCompute(
  user: Pick<AuthUser, "role" | "is_active"> | null | undefined
): boolean {
  return isAdminUser(user);
}

export type InviteBadgeStatus = "active" | "exhausted" | "expired" | "revoked";

export interface InviteStatusBadge {
  status: InviteBadgeStatus;
  label: string;
  tone: "emerald" | "amber" | "red" | "muted";
  canRevoke: boolean;
  remainingUses: number;
}

/**
 * Classify a friend invite code into its display badge status.
 * Precedence: revoked > expired > exhausted > active.
 */
export function classifyInviteStatus(
  invite: Pick<
    AdminInviteItem,
    "max_uses" | "use_count" | "expires_at" | "revoked_at"
  >,
  nowMs = Date.now()
): InviteStatusBadge {
  const maxUses = Math.max(1, Number(invite.max_uses ?? 1));
  const useCount = Math.max(0, Number(invite.use_count ?? 0));
  const remainingUses = Math.max(0, maxUses - useCount);

  if (invite.revoked_at) {
    return {
      status: "revoked",
      label: "Revoked",
      tone: "red",
      canRevoke: false,
      remainingUses: 0,
    };
  }

  if (invite.expires_at) {
    const expMs = Date.parse(invite.expires_at);
    if (Number.isFinite(expMs) && expMs <= nowMs) {
      return {
        status: "expired",
        label: "Expired",
        tone: "muted",
        canRevoke: true,
        remainingUses: 0,
      };
    }
  }

  if (useCount >= maxUses) {
    return {
      status: "exhausted",
      label: "Exhausted",
      tone: "amber",
      canRevoke: true,
      remainingUses: 0,
    };
  }

  return {
    status: "active",
    label: "Active",
    tone: "emerald",
    canRevoke: true,
    remainingUses,
  };
}

export interface StorageQuotaSummary {
  usedBytes: number;
  quotaBytes: number;
  pct: number;
  clampedPct: number;
  usedLabel: string;
  quotaLabel: string;
  pctLabel: string;
  tone: "emerald" | "amber" | "red";
}

export function formatStorageBytes(bytes: number | null | undefined): string {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

/**
 * Format upload disk usage vs quota bar metrics & threshold tone.
 */
export function computeStorageQuotaSummary(
  usedBytes: number | null | undefined,
  quotaBytes: number | null | undefined,
  explicitPct?: number | null
): StorageQuotaSummary {
  const safeUsed =
    typeof usedBytes === "number" && Number.isFinite(usedBytes) && usedBytes > 0
      ? usedBytes
      : 0;
  const safeQuota =
    typeof quotaBytes === "number" &&
    Number.isFinite(quotaBytes) &&
    quotaBytes > 0
      ? quotaBytes
      : 0;

  let pct = 0;
  if (
    typeof explicitPct === "number" &&
    Number.isFinite(explicitPct) &&
    explicitPct >= 0
  ) {
    pct = explicitPct;
  } else if (safeQuota > 0) {
    pct = (safeUsed / safeQuota) * 100;
  }

  const roundedPct = Math.round(pct * 10) / 10;
  const clampedPct = Math.max(0, Math.min(100, roundedPct));
  const tone: StorageQuotaSummary["tone"] =
    roundedPct >= 90 ? "red" : roundedPct >= 70 ? "amber" : "emerald";

  return {
    usedBytes: safeUsed,
    quotaBytes: safeQuota,
    pct: roundedPct,
    clampedPct,
    usedLabel: formatStorageBytes(safeUsed),
    quotaLabel: safeQuota > 0 ? formatStorageBytes(safeQuota) : "No quota",
    pctLabel: safeQuota > 0 ? `${roundedPct.toFixed(1)}%` : "—",
    tone,
  };
}

export interface NormalizedTrafficMetrics {
  requests: number;
  errors: number;
  rateLimited: number;
  bytesOut: number;
}

export function normalizeTrafficWindow(
  raw: AdminTrafficWindow | null | undefined
): NormalizedTrafficMetrics {
  if (!raw) {
    return { requests: 0, errors: 0, rateLimited: 0, bytesOut: 0 };
  }
  return {
    requests: Number(raw.requests ?? raw.request_count ?? 0) || 0,
    errors: Number(raw.errors ?? raw.error_count ?? 0) || 0,
    rateLimited: Number(raw.rate_limited ?? raw.rate_limited_count ?? 0) || 0,
    bytesOut: Number(raw.bytes_out ?? 0) || 0,
  };
}

export interface NormalizedRouteRow {
  routeGroup: string;
  requests: number;
  errors: number;
  rateLimited: number;
  bytesOut: number;
}

export function normalizeRouteBreakdown(
  raw:
    | AdminRouteBreakdownItem[]
    | Record<string, Omit<AdminRouteBreakdownItem, "route_group">>
    | null
    | undefined
): NormalizedRouteRow[] {
  if (!raw) return [];
  const rows: NormalizedRouteRow[] = Array.isArray(raw)
    ? raw.map((item) => ({
        routeGroup: String(item.route_group ?? "other"),
        requests:
          Number(item.requests ?? item.request_count ?? item.requests_24h ?? 0) ||
          0,
        errors:
          Number(item.errors ?? item.error_count ?? item.errors_24h ?? 0) || 0,
        rateLimited:
          Number(
            item.rate_limited ??
              item.rate_limited_count ??
              item.rate_limited_24h ??
              0
          ) || 0,
        bytesOut: Number(item.bytes_out ?? 0) || 0,
      }))
    : Object.entries(raw).map(([routeGroup, item]) => ({
        routeGroup,
        requests:
          Number(item.requests ?? item.request_count ?? item.requests_24h ?? 0) ||
          0,
        errors:
          Number(item.errors ?? item.error_count ?? item.errors_24h ?? 0) || 0,
        rateLimited:
          Number(
            item.rate_limited ??
              item.rate_limited_count ??
              item.rate_limited_24h ??
              0
          ) || 0,
        bytesOut: Number(item.bytes_out ?? 0) || 0,
      }));

  return rows.sort((a, b) => b.requests - a.requests);
}

/**
 * Translate HTTP 429 / 403 / 507 API errors into crisp user-facing messages.
 */
export function formatFriendlyApiError(
  err: unknown,
  fallback = "Operation failed"
): string {
  const status =
    err && typeof err === "object" && "status" in err
      ? Number((err as { status: unknown }).status)
      : null;
  const rawMessage =
    err instanceof Error && err.message.trim() ? err.message.trim() : "";
  const lower = rawMessage.toLowerCase();

  if (status === 429) {
    return "Too many requests — please wait a moment and try again.";
  }

  if (status === 403) {
    if (
      lower.includes("suspend") ||
      lower.includes("disabled") ||
      lower.includes("inactive")
    ) {
      return "Account suspended — please contact the instance administrator.";
    }
    if (lower.includes("closed")) {
      return "Registration is currently closed — public browsing remains open.";
    }
    if (lower.includes("invite")) {
      return rawMessage.length > 0 && rawMessage.length < 140
        ? `Invite code required: ${rawMessage}`
        : "A valid friend invite code is required to create an account.";
    }
    if (lower.includes("admin")) {
      return "Admin privileges are required for this action.";
    }
    return rawMessage || "Access denied (403).";
  }

  if (status === 507) {
    return "Upload storage quota reached — please contact the instance admin.";
  }

  return rawMessage || fallback;
}

export function formatCompactCount(value: number | null | undefined): string {
  const n = typeof value === "number" && Number.isFinite(value) ? value : 0;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${(n / 1_000).toFixed(1)}k`;
  return Math.round(n).toLocaleString("en-AU");
}

export function formatShortTimestamp(iso: string | null | undefined): string {
  if (!iso) return "Never";
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return iso;
  return new Date(ms).toISOString().slice(0, 16).replace("T", " ") + " UTC";
}
