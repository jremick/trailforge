import {
  formatFriendlyApiError,
  formatShortTimestamp,
  resolveApiUrl,
} from "../adminHelpers";
import { ApiError, getStoredAuthToken } from "../api";
import { escapeHtml } from "../presets";

export type AiTokenScope = "trailforge:read" | "trailforge:write";

export interface PersonalAccessTokenItem {
  id: string;
  name: string;
  token_prefix: string;
  scopes: string[];
  expires_at?: string | null;
  last_used_at?: string | null;
  revoked_at?: string | null;
  created_at?: string | null;
}

export interface CreatedPersonalAccessToken extends PersonalAccessTokenItem {
  token: string;
}

export interface CreatePersonalAccessTokenInput {
  name: string;
  scopes: AiTokenScope[];
  expires_in_days?: number | null;
}

export interface OAuthGrantItem {
  client_id: string;
  client_name: string;
  scopes: string[];
  active_token_count?: number;
  created_at?: string | null;
  last_used_at?: string | null;
  expires_at?: string | null;
}

export interface McpConnectionUrls {
  publicMcpUrl: string;
  authMcpUrl: string;
  stdioSnippet: string;
}

export interface AiTokensApiClient {
  listTokens(signal?: AbortSignal): Promise<PersonalAccessTokenItem[]>;
  createToken(
    input: CreatePersonalAccessTokenInput
  ): Promise<CreatedPersonalAccessToken>;
  revokeToken(tokenId: string): Promise<void>;
  listOAuthGrants(signal?: AbortSignal): Promise<OAuthGrantItem[]>;
  revokeOAuthGrant(clientId: string): Promise<void>;
}

export interface AiTokensSectionOptions {
  /** Canonical web origin (e.g. `window.location.origin`), defaults to `https://trails.example.com` when unavailable. */
  origin?: string;
  /** Optional custom API client (defaults to live `/api/auth/tokens` & `/api/auth/oauth-grants` fetch client). */
  api?: AiTokensApiClient;
  /** Optional clipboard writer override for tests or embedded hosts. */
  copyText?: (value: string) => Promise<void>;
}

export interface AiTokensViewState {
  loading: boolean;
  busy: boolean;
  disabledByServer: boolean;
  error: string | null;
  notice: string | null;
  tokens: PersonalAccessTokenItem[];
  grants: OAuthGrantItem[];
  newlyMintedToken: CreatedPersonalAccessToken | null;
  urls: McpConnectionUrls;
}

const VALID_SCOPES: ReadonlySet<string> = new Set([
  "trailforge:read",
  "trailforge:write",
]);

/**
 * Normalize requested PAT scopes, always ensuring `trailforge:read` is included
 * when `trailforge:write` is granted (per SPEC_AI_MCP Section 5.1).
 */
export function normalizeRequestedScopes(
  rawScopes: readonly string[]
): AiTokenScope[] {
  const set = new Set<AiTokenScope>();
  for (const s of rawScopes) {
    const trimmed = s.trim();
    if (trimmed === "trailforge:read" || trimmed === "trailforge:write") {
      set.add(trimmed);
    }
  }
  if (set.has("trailforge:write") || set.size === 0) {
    set.add("trailforge:read");
  }
  return set.has("trailforge:write")
    ? ["trailforge:read", "trailforge:write"]
    : ["trailforge:read"];
}

/**
 * Derive clean MCP endpoint URLs and local `stdio` bridge snippet from the current origin.
 */
export function buildMcpConnectionUrls(originRaw?: string): McpConnectionUrls {
  const fallback = "https://trails.example.com";
  const cleaned = (originRaw ?? "").trim().replace(/\/+$/, "");
  const origin =
    cleaned && /^https?:\/\//i.test(cleaned) ? cleaned : fallback;
  const publicMcpUrl = `${origin}/mcp`;
  const authMcpUrl = `${origin}/mcp/auth`;
  const stdioSnippet = `TRAILFORGE_MCP_URL="${publicMcpUrl}" TRAILFORGE_API_TOKEN="<paste-tf_pat-token>" trailforge-mcp`;
  return { publicMcpUrl, authMcpUrl, stdioSnippet };
}

/**
 * Classify a PAT as active, revoked, or expired.
 */
export function classifyPatStatus(
  token: Pick<PersonalAccessTokenItem, "revoked_at" | "expires_at">,
  nowMs = Date.now()
): { status: "active" | "revoked" | "expired"; label: string } {
  if (token.revoked_at) {
    return { status: "revoked", label: "Revoked" };
  }
  if (token.expires_at) {
    const expMs = Date.parse(token.expires_at);
    if (Number.isFinite(expMs) && expMs <= nowMs) {
      return { status: "expired", label: "Expired" };
    }
  }
  return { status: "active", label: "Active" };
}

function normalizeScopeList(raw: unknown): string[] {
  if (Array.isArray(raw)) {
    return raw
      .map((item) => String(item).trim())
      .filter((s) => VALID_SCOPES.has(s) || s.length > 0);
  }
  if (typeof raw === "string" && raw.trim()) {
    return raw
      .trim()
      .split(/\s+/)
      .filter((s) => s.length > 0);
  }
  return ["trailforge:read"];
}

export function normalizePatItem(raw: unknown): PersonalAccessTokenItem | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const id = String(obj.id ?? obj.token_id ?? "").trim();
  if (!id) return null;
  return {
    id,
    name: String(obj.name ?? "Personal Access Token").trim() || "Personal Access Token",
    token_prefix: String(obj.token_prefix ?? obj.prefix ?? "tf_pat_").trim(),
    scopes: normalizeScopeList(obj.scopes ?? obj.scope),
    expires_at: typeof obj.expires_at === "string" ? obj.expires_at : null,
    last_used_at: typeof obj.last_used_at === "string" ? obj.last_used_at : null,
    revoked_at: typeof obj.revoked_at === "string" ? obj.revoked_at : null,
    created_at: typeof obj.created_at === "string" ? obj.created_at : null,
  };
}

export function normalizeCreatedPatResponse(
  raw: unknown
): CreatedPersonalAccessToken {
  if (!raw || typeof raw !== "object") {
    throw new Error("Invalid Personal Access Token response from server.");
  }
  const obj = raw as Record<string, unknown>;
  const plaintext = String(
    obj.token ?? obj.plaintext_token ?? obj.access_token ?? ""
  ).trim();
  const baseCandidate =
    obj.item && typeof obj.item === "object"
      ? obj.item
      : obj.token_record && typeof obj.token_record === "object"
        ? obj.token_record
        : obj;
  const base = normalizePatItem(baseCandidate);
  if (!plaintext) {
    throw new Error("Server did not return a plaintext token value.");
  }
  return {
    id: base?.id ?? `pat_${Date.now()}`,
    name: base?.name ?? String(obj.name ?? "Personal Access Token"),
    token_prefix:
      base?.token_prefix ??
      plaintext.slice(0, Math.min(14, plaintext.length)),
    scopes: base?.scopes ?? normalizeScopeList(obj.scopes ?? obj.scope),
    expires_at: base?.expires_at ?? null,
    last_used_at: base?.last_used_at ?? null,
    revoked_at: base?.revoked_at ?? null,
    created_at: base?.created_at ?? new Date().toISOString(),
    token: plaintext,
  };
}

export function normalizeOAuthGrantItem(raw: unknown): OAuthGrantItem | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const clientId = String(obj.client_id ?? "").trim();
  if (!clientId) return null;
  return {
    client_id: clientId,
    client_name:
      String(obj.client_name ?? obj.name ?? clientId).trim() || clientId,
    scopes: normalizeScopeList(obj.scopes ?? obj.scope),
    active_token_count:
      typeof obj.active_token_count === "number"
        ? obj.active_token_count
        : undefined,
    created_at: typeof obj.created_at === "string" ? obj.created_at : null,
    last_used_at: typeof obj.last_used_at === "string" ? obj.last_used_at : null,
    expires_at: typeof obj.expires_at === "string" ? obj.expires_at : null,
  };
}

function extractListArray(payload: unknown, keys: readonly string[]): unknown[] {
  if (Array.isArray(payload)) return payload;
  if (payload && typeof payload === "object") {
    const obj = payload as Record<string, unknown>;
    for (const k of keys) {
      if (Array.isArray(obj[k])) return obj[k] as unknown[];
    }
  }
  return [];
}

async function parseApiErrorMessage(res: Response): Promise<string> {
  try {
    const ct = res.headers.get("content-type") ?? "";
    if (ct.includes("application/json")) {
      const body = (await res.json()) as {
        detail?: unknown;
        message?: unknown;
      };
      if (typeof body.detail === "string" && body.detail.trim()) {
        return body.detail.trim();
      }
      if (typeof body.message === "string" && body.message.trim()) {
        return body.message.trim();
      }
    }
    const text = await res.text();
    if (text.trim()) return text.trim().slice(0, 200);
  } catch (err) {
    console.debug("Unable to parse AI tokens API error response:", err);
  }
  return `HTTP ${res.status} ${res.statusText}`;
}

/**
 * Default fetch-backed client for `/api/auth/tokens` and `/api/auth/oauth-grants`.
 * Uses `credentials: "include"` and optional Bearer only when `getStoredAuthToken()`
 * allows it (cross-origin / native Capacitor — strictly cookie-only on same-origin web).
 */
export function createAiTokensApiClient(
  fetchImpl: typeof fetch = fetch
): AiTokensApiClient {
  const request = async <T>(
    path: string,
    options?: { method?: string; body?: unknown; signal?: AbortSignal }
  ): Promise<T> => {
    const hasBody = options?.body !== undefined;
    const headers: Record<string, string> = {
      Accept: "application/json",
    };
    if (hasBody) {
      headers["Content-Type"] = "application/json";
    }
    const storedToken = getStoredAuthToken();
    if (storedToken) {
      headers.Authorization = `Bearer ${storedToken}`;
    }

    const res = await fetchImpl(resolveApiUrl(path), {
      method: options?.method ?? "GET",
      headers,
      credentials: "include",
      cache: "no-store",
      body: hasBody ? JSON.stringify(options.body) : undefined,
      signal: options?.signal,
    });

    if (!res.ok) {
      const detail = await parseApiErrorMessage(res);
      throw new ApiError(detail, res.status);
    }
    if (res.status === 204) {
      return undefined as T;
    }
    return (await res.json()) as T;
  };

  return {
    async listTokens(signal?: AbortSignal): Promise<PersonalAccessTokenItem[]> {
      const raw = await request<unknown>("/api/auth/tokens", { signal });
      return extractListArray(raw, ["items", "tokens"])
        .map(normalizePatItem)
        .filter((item): item is PersonalAccessTokenItem => item !== null);
    },

    async createToken(
      input: CreatePersonalAccessTokenInput
    ): Promise<CreatedPersonalAccessToken> {
      const body: Record<string, unknown> = {
        name: input.name.trim(),
        scopes: normalizeRequestedScopes(input.scopes),
      };
      if (
        typeof input.expires_in_days === "number" &&
        Number.isFinite(input.expires_in_days) &&
        input.expires_in_days > 0
      ) {
        body.expires_in_days = Math.round(input.expires_in_days);
      }
      const raw = await request<unknown>("/api/auth/tokens", {
        method: "POST",
        body,
      });
      return normalizeCreatedPatResponse(raw);
    },

    async revokeToken(tokenId: string): Promise<void> {
      await request<void>(`/api/auth/tokens/${encodeURIComponent(tokenId)}`, {
        method: "DELETE",
      });
    },

    async listOAuthGrants(signal?: AbortSignal): Promise<OAuthGrantItem[]> {
      const raw = await request<unknown>("/api/auth/oauth-grants", { signal });
      return extractListArray(raw, ["items", "grants"])
        .map(normalizeOAuthGrantItem)
        .filter((item): item is OAuthGrantItem => item !== null);
    },

    async revokeOAuthGrant(clientId: string): Promise<void> {
      await request<void>(
        `/api/auth/oauth-grants/${encodeURIComponent(clientId)}`,
        {
          method: "DELETE",
        }
      );
    },
  };
}

function formatScopeBadgesHtml(scopes: readonly string[]): string {
  return scopes
    .map((scope) => {
      const isWrite = scope === "trailforge:write";
      const label = isWrite
        ? "write"
        : scope === "trailforge:read"
          ? "read"
          : scope;
      const cls = isWrite ? "tf-mini-tag amber" : "tf-mini-tag cyan";
      return `<span class="${cls} mono">${escapeHtml(label)}</span>`;
    })
    .join(" ");
}

/**
 * Pure HTML renderer for the Account Panel "AI Assistants & MCP Tokens" section.
 * Reuses TrailForge's native `tf-insp-section`, `tf-vehicle-card`, `tf-form`,
 * `tf-mini-btn`, and `tf-privacy-note` classes so it integrates seamlessly with
 * `accountPanel.ts` without extra stylesheet dependencies.
 */
export function renderAiTokensSectionHtml(state: AiTokensViewState): string {
  const { urls } = state;

  const endpointsHtml = `
    <section class="tf-insp-section" data-role="ai-mcp-endpoints">
      <div class="tf-insp-sec-head">
        <h3>AI Assistants &amp; MCP</h3>
        <span class="mono tf-insp-meta">SEP-1865</span>
      </div>
      <p class="tf-inspector-muted">
        Connect ChatGPT, Claude, Antigravity, Gemini, or Cursor to TrailForge trail intelligence and interactive maps.
      </p>
      <div class="tf-card-stack">
        <div class="tf-vehicle-card">
          <div class="tf-vehicle-head">
            <div>
              <div class="tf-vehicle-name">Public / PAT Endpoint</div>
              <div class="tf-vehicle-specs mono" style="word-break:break-all">${escapeHtml(urls.publicMcpUrl)}</div>
            </div>
            <button type="button" class="tf-mini-btn" data-copy-value="${escapeHtml(urls.publicMcpUrl)}" data-copy-label="Public MCP URL">Copy</button>
          </div>
        </div>
        <div class="tf-vehicle-card">
          <div class="tf-vehicle-head">
            <div>
              <div class="tf-vehicle-name">OAuth 2.1 Sign-In Endpoint</div>
              <div class="tf-vehicle-specs mono" style="word-break:break-all">${escapeHtml(urls.authMcpUrl)}</div>
            </div>
            <button type="button" class="tf-mini-btn" data-copy-value="${escapeHtml(urls.authMcpUrl)}" data-copy-label="OAuth MCP URL">Copy</button>
          </div>
        </div>
      </div>
    </section>
  `;

  if (state.disabledByServer) {
    return `
      ${endpointsHtml}
      <section class="tf-insp-section" data-role="ai-tokens-disabled">
        <div class="tf-privacy-note" role="status">
          AI / MCP token management is currently disabled on this TrailForge server.
        </div>
      </section>
    `;
  }

  const errorBannerHtml = state.error
    ? `<div class="tf-inspector-error" role="alert">${escapeHtml(state.error)}</div>`
    : "";

  const noticeBannerHtml = state.notice
    ? `<div class="tf-privacy-note" role="status">${escapeHtml(state.notice)}</div>`
    : "";

  const mintedBannerHtml = state.newlyMintedToken
    ? `
      <div class="tf-privacy-note" data-role="pat-minted-banner" role="status" style="border-color:rgba(245,158,11,0.55);background:rgba(245,158,11,0.10)">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px">
          <strong>Token created: ${escapeHtml(state.newlyMintedToken.name)}</strong>
          <button type="button" class="tf-mini-btn" data-role="dismiss-minted">Dismiss</button>
        </div>
        <p style="margin:0 0 6px;font-size:11.5px">
          Copy this Personal Access Token now — for security it is stored only as a SHA-256 hash and will <strong>never be shown again</strong>.
        </p>
        <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
          <code class="mono" data-role="minted-token-value" style="flex:1;padding:6px 8px;border-radius:4px;background:rgba(11,17,23,0.9);border:1px solid rgba(56,189,248,0.35);word-break:break-all;font-size:11px;color:#FDE047">${escapeHtml(state.newlyMintedToken.token)}</code>
          <button type="button" class="tf-mini-btn active" data-copy-value="${escapeHtml(state.newlyMintedToken.token)}" data-copy-label="Personal Access Token">Copy</button>
        </div>
        <button type="button" class="tf-mini-btn" data-copy-value="${escapeHtml(`TRAILFORGE_MCP_URL="${urls.publicMcpUrl}" TRAILFORGE_API_TOKEN="${state.newlyMintedToken.token}" trailforge-mcp`)}" data-copy-label="Local stdio CLI command">Copy local CLI snippet</button>
      </div>
    `
    : "";

  const activeTokens = state.tokens.filter(
    (t) => classifyPatStatus(t).status === "active"
  );

  const tokensListHtml = state.loading
    ? `<p class="tf-inspector-muted">Loading Personal Access Tokens…</p>`
    : state.tokens.length > 0
      ? state.tokens
          .map((tok) => {
            const st = classifyPatStatus(tok);
            const metaParts = [
              `Created ${formatShortTimestamp(tok.created_at)}`,
              tok.last_used_at
                ? `Last used ${formatShortTimestamp(tok.last_used_at)}`
                : "Never used",
              tok.expires_at
                ? `Expires ${formatShortTimestamp(tok.expires_at)}`
                : "No expiry",
            ].join(" · ");
            return `
              <div class="tf-vehicle-card ${st.status === "active" ? "" : "tf-muted-card"}" data-pat-id="${escapeHtml(tok.id)}">
                <div class="tf-vehicle-head">
                  <div>
                    <div class="tf-vehicle-name">${escapeHtml(tok.name)}</div>
                    <div class="tf-vehicle-specs mono">${escapeHtml(tok.token_prefix)}… · ${formatScopeBadgesHtml(tok.scopes)}</div>
                  </div>
                  <span class="tf-pill ${st.status === "active" ? "tf-pill-ice" : "tf-pill-amber"}">${escapeHtml(st.label)}</span>
                </div>
                <div class="tf-vehicle-reasons">${escapeHtml(metaParts)}</div>
                ${
                  st.status === "active"
                    ? `
                      <div class="tf-vehicle-actions">
                        <button type="button" class="tf-mini-btn danger" data-revoke-pat="${escapeHtml(tok.id)}" ${state.busy ? "disabled" : ""}>Revoke Token</button>
                      </div>
                    `
                    : ""
                }
              </div>
            `;
          })
          .join("")
      : `<p class="tf-inspector-muted">No Personal Access Tokens yet. Create a scoped token below for Claude Code, Cursor, VS Code, or local CLI access.</p>`;

  const grantsListHtml = state.loading
    ? `<p class="tf-inspector-muted">Loading connected AI apps…</p>`
    : state.grants.length > 0
      ? state.grants
          .map((grant) => {
            const metaParts = [
              grant.last_used_at
                ? `Last used ${formatShortTimestamp(grant.last_used_at)}`
                : grant.created_at
                  ? `Connected ${formatShortTimestamp(grant.created_at)}`
                  : "Authorized",
              typeof grant.active_token_count === "number"
                ? `${grant.active_token_count} active session${grant.active_token_count === 1 ? "" : "s"}`
                : null,
            ]
              .filter(Boolean)
              .join(" · ");
            return `
              <div class="tf-vehicle-card" data-oauth-client="${escapeHtml(grant.client_id)}">
                <div class="tf-vehicle-head">
                  <div>
                    <div class="tf-vehicle-name">${escapeHtml(grant.client_name)}</div>
                    <div class="tf-vehicle-specs">${formatScopeBadgesHtml(grant.scopes)}</div>
                  </div>
                  <span class="tf-pill tf-pill-ice">OAuth 2.1</span>
                </div>
                <div class="tf-vehicle-reasons">${escapeHtml(metaParts)}</div>
                <div class="tf-vehicle-actions">
                  <button type="button" class="tf-mini-btn danger" data-revoke-grant="${escapeHtml(grant.client_id)}" ${state.busy ? "disabled" : ""}>Revoke Access</button>
                </div>
              </div>
            `;
          })
          .join("")
      : `<p class="tf-inspector-muted">No OAuth AI apps connected yet. Signing in via ChatGPT, Claude Web, or Gemini Enterprise will list the app here.</p>`;

  return `
    ${endpointsHtml}
    ${errorBannerHtml}
    ${noticeBannerHtml}
    ${mintedBannerHtml}

    <section class="tf-insp-section" data-role="ai-pat-section">
      <div class="tf-insp-sec-head">
        <h3>Personal Access Tokens</h3>
        <span class="mono tf-insp-meta">${activeTokens.length} active</span>
      </div>

      <form class="tf-form" data-role="pat-create-form">
        <label class="tf-field">
          <span class="tf-label">Token Name</span>
          <input type="text" class="tf-input" name="name" required maxlength="80" placeholder="e.g. Claude Code Laptop" />
        </label>

        <div class="tf-field-row">
          <label class="tf-field">
            <span class="tf-label">Scope</span>
            <select class="tf-select" name="scope_preset">
              <option value="read">Read Only (trailforge:read)</option>
              <option value="read_write">Read &amp; Write (save routes/pins)</option>
            </select>
          </label>
          <label class="tf-field">
            <span class="tf-label">Expiry</span>
            <select class="tf-select" name="expires_in_days">
              <option value="30">30 days</option>
              <option value="90" selected>90 days</option>
              <option value="365">1 year</option>
              <option value="0">Never</option>
            </select>
          </label>
        </div>

        <button type="submit" class="tf-btn-primary" ${state.busy ? "disabled" : ""}>+ Create Personal Access Token</button>
      </form>

      <div class="tf-card-stack" style="margin-top:10px">${tokensListHtml}</div>
    </section>

    <section class="tf-insp-section" data-role="ai-oauth-grants-section">
      <div class="tf-insp-sec-head">
        <h3>Connected AI Apps (OAuth)</h3>
        <span class="mono tf-insp-meta">${state.grants.length}</span>
      </div>
      <div class="tf-card-stack">${grantsListHtml}</div>
    </section>
  `;
}

async function defaultCopyText(value: string): Promise<void> {
  if (
    typeof navigator !== "undefined" &&
    navigator.clipboard &&
    typeof navigator.clipboard.writeText === "function"
  ) {
    await navigator.clipboard.writeText(value);
    return;
  }
  throw new Error("Clipboard API unavailable in this browser context.");
}

/**
 * Stateful, self-contained controller for the Account Panel AI Tokens & Connected Apps UI.
 * Integrator mounts this inside `RigAccountPanel.renderAccountTab()` / `bindEvents()`
 * in ~5 lines without modifying any other Account Panel logic.
 */
export class AiTokensSection {
  private readonly api: AiTokensApiClient;
  private readonly copyText: (value: string) => Promise<void>;
  private container: HTMLElement | null = null;
  private loaded = false;
  private abortController: AbortController | null = null;
  private state: AiTokensViewState;

  constructor(options: AiTokensSectionOptions = {}) {
    this.api = options.api ?? createAiTokensApiClient();
    this.copyText = options.copyText ?? defaultCopyText;
    const detectedOrigin =
      options.origin ??
      (typeof window !== "undefined" ? window.location?.origin : undefined);
    this.state = {
      loading: false,
      busy: false,
      disabledByServer: false,
      error: null,
      notice: null,
      tokens: [],
      grants: [],
      newlyMintedToken: null,
      urls: buildMcpConnectionUrls(detectedOrigin),
    };
  }

  getState(): Readonly<AiTokensViewState> {
    return this.state;
  }

  /**
   * Mount into a DOM container inside Account Panel. Preserves in-memory state
   * (including a newly minted one-time plaintext token) across parent re-renders,
   * and triggers initial fetch on first mount.
   */
  mount(container: HTMLElement): void {
    this.container = container;
    this.render();
    if (!this.loaded && !this.state.loading) {
      void this.refresh();
    }
  }

  /**
   * Reset all in-memory token/grant state (call on user logout to clear secrets).
   */
  reset(): void {
    this.abortController?.abort();
    this.abortController = null;
    this.loaded = false;
    this.state = {
      ...this.state,
      loading: false,
      busy: false,
      disabledByServer: false,
      error: null,
      notice: null,
      tokens: [],
      grants: [],
      newlyMintedToken: null,
    };
    this.render();
  }

  async refresh(): Promise<void> {
    this.abortController?.abort();
    const ac = new AbortController();
    this.abortController = ac;
    this.state = { ...this.state, loading: true, error: null };
    this.render();

    try {
      const [tokens, grants] = await Promise.all([
        this.api.listTokens(ac.signal),
        this.api.listOAuthGrants(ac.signal),
      ]);
      if (ac.signal.aborted) return;
      this.loaded = true;
      this.state = {
        ...this.state,
        loading: false,
        disabledByServer: false,
        tokens,
        grants,
      };
    } catch (err) {
      if (ac.signal.aborted) return;
      if (err instanceof ApiError && err.status === 404) {
        this.loaded = true;
        this.state = {
          ...this.state,
          loading: false,
          disabledByServer: true,
          tokens: [],
          grants: [],
        };
      } else {
        this.state = {
          ...this.state,
          loading: false,
          error: formatFriendlyApiError(err, "Failed to load AI tokens"),
        };
      }
    } finally {
      if (!ac.signal.aborted) {
        this.render();
      }
    }
  }

  async createToken(input: CreatePersonalAccessTokenInput): Promise<void> {
    const cleanName = input.name.trim();
    if (!cleanName) {
      this.state = { ...this.state, error: "Please enter a token name." };
      this.render();
      return;
    }

    this.state = { ...this.state, busy: true, error: null, notice: null };
    this.render();

    try {
      const created = await this.api.createToken({
        name: cleanName,
        scopes: normalizeRequestedScopes(input.scopes),
        expires_in_days: input.expires_in_days ?? null,
      });
      const tokens = await this.api.listTokens();
      this.loaded = true;
      this.state = {
        ...this.state,
        busy: false,
        tokens,
        newlyMintedToken: created,
        notice: null,
      };
    } catch (err) {
      this.state = {
        ...this.state,
        busy: false,
        error: formatFriendlyApiError(err, "Could not create access token"),
      };
    } finally {
      this.render();
    }
  }

  async revokeToken(tokenId: string): Promise<void> {
    this.state = { ...this.state, busy: true, error: null, notice: null };
    this.render();

    try {
      await this.api.revokeToken(tokenId);
      const tokens = await this.api.listTokens();
      const clearedMinted =
        this.state.newlyMintedToken?.id === tokenId
          ? null
          : this.state.newlyMintedToken;
      this.state = {
        ...this.state,
        busy: false,
        tokens,
        newlyMintedToken: clearedMinted,
        notice: "Personal Access Token revoked immediately.",
      };
    } catch (err) {
      this.state = {
        ...this.state,
        busy: false,
        error: formatFriendlyApiError(err, "Could not revoke access token"),
      };
    } finally {
      this.render();
    }
  }

  async revokeOAuthGrant(clientId: string): Promise<void> {
    this.state = { ...this.state, busy: true, error: null, notice: null };
    this.render();

    try {
      await this.api.revokeOAuthGrant(clientId);
      const grants = await this.api.listOAuthGrants();
      this.state = {
        ...this.state,
        busy: false,
        grants,
        notice: "Connected AI app access revoked.",
      };
    } catch (err) {
      this.state = {
        ...this.state,
        busy: false,
        error: formatFriendlyApiError(err, "Could not revoke connected app"),
      };
    } finally {
      this.render();
    }
  }

  dismissMintedToken(): void {
    this.state = { ...this.state, newlyMintedToken: null };
    this.render();
  }

  private render(): void {
    if (!this.container) return;
    this.container.innerHTML = renderAiTokensSectionHtml(this.state);
    this.bindEvents();
  }

  private bindEvents(): void {
    if (!this.container) return;

    this.container
      .querySelector<HTMLButtonElement>("[data-role='dismiss-minted']")
      ?.addEventListener("click", () => {
        this.dismissMintedToken();
      });

    this.container
      .querySelectorAll<HTMLButtonElement>("[data-copy-value]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const val = btn.dataset.copyValue ?? "";
          const label = btn.dataset.copyLabel ?? "Value";
          void this.copyText(val)
            .then(() => {
              this.state = {
                ...this.state,
                error: null,
                notice: `Copied ${label} to clipboard.`,
              };
              this.render();
            })
            .catch((err) => {
              console.warn("Clipboard write failed:", err);
              this.state = {
                ...this.state,
                error: "Could not copy to clipboard — please select and copy manually.",
              };
              this.render();
            });
        });
      });

    this.container
      .querySelector<HTMLFormElement>("[data-role='pat-create-form']")
      ?.addEventListener("submit", (e) => {
        e.preventDefault();
        const form = e.currentTarget as HTMLFormElement;
        const fd = new FormData(form);
        const name = String(fd.get("name") ?? "").trim();
        const preset = String(fd.get("scope_preset") ?? "read");
        const scopes: AiTokenScope[] =
          preset === "read_write"
            ? ["trailforge:read", "trailforge:write"]
            : ["trailforge:read"];
        const rawDays = Number(fd.get("expires_in_days") ?? 90);
        const expiresInDays =
          Number.isFinite(rawDays) && rawDays > 0 ? rawDays : null;
        void this.createToken({ name, scopes, expires_in_days: expiresInDays });
      });

    this.container
      .querySelectorAll<HTMLButtonElement>("[data-revoke-pat]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const id = btn.dataset.revokePat;
          if (id) void this.revokeToken(id);
        });
      });

    this.container
      .querySelectorAll<HTMLButtonElement>("[data-revoke-grant]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const clientId = btn.dataset.revokeGrant;
          if (clientId) void this.revokeOAuthGrant(clientId);
        });
      });
  }
}

/**
 * Convenience mount helper for single-line mounting in Account Panel.
 */
export function mountAiTokensSection(
  container: HTMLElement,
  options?: AiTokensSectionOptions
): AiTokensSection {
  const section = new AiTokensSection(options);
  section.mount(container);
  return section;
}
