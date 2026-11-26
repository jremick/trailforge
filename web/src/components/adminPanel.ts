import {
  classifyInviteStatus,
  computeStorageQuotaSummary,
  formatCompactCount,
  formatFriendlyApiError,
  formatShortTimestamp,
  formatStorageBytes,
  normalizeRouteBreakdown,
  normalizeTrafficWindow,
} from "../adminHelpers";
import {
  CANONICAL_LAYER_ORDER,
  DEFAULT_LAYER_VISIBILITY,
  LAYER_DEFINITION_MAP,
  moveLayerInOrder,
  normalizeLayerOrder,
  normalizeLayerVisibility,
  reorderLayerKeys,
} from "../layerPrefsHelpers";
import { escapeHtml } from "../presets";
import type {
  AdminInviteCreateInput,
  AdminInviteItem,
  AdminStatsResponse,
  AdminUiSettingsResponse,
  AdminUiSettingsUpdateInput,
  AdminUserItem,
  AdminUserPatchInput,
  LayerKey,
  LayerVisibilityMap,
  RegistrationMode,
  UserRole,
} from "../types";

export type AdminPanelTab = "overview" | "invites" | "users" | "settings";

export interface AdminPanelState {
  currentUserId: string | null;
  registrationMode: RegistrationMode;
  loading: boolean;
  stats: AdminStatsResponse | null;
  invites: AdminInviteItem[];
  users: AdminUserItem[];
  uiSettings: AdminUiSettingsResponse | null;
  latestCreatedInvite: AdminInviteItem | null;
  busyAction: string | null;
  error: string | null;
}

export interface AdminPanelCallbacks {
  onClose: () => void;
  onRefresh: () => Promise<void>;
  onCreateInvite: (payload: AdminInviteCreateInput) => Promise<void>;
  onRevokeInvite: (inviteId: string) => Promise<void>;
  onCopyInviteCode: (code: string) => Promise<void>;
  onPatchUser: (userId: string, patch: AdminUserPatchInput) => Promise<void>;
  onSyncClosures: () => Promise<void>;
  onRecomputeGrades: () => Promise<void>;
  onSaveUiSettings: (payload: AdminUiSettingsUpdateInput) => Promise<void>;
  onResetUiSettings: () => Promise<void>;
}

export class AdminConsolePanel {
  private readonly root: HTMLElement;
  private readonly callbacks: AdminPanelCallbacks;
  private state: AdminPanelState;
  private isOpen = false;
  private activeTab: AdminPanelTab = "overview";
  private copiedCode: string | null = null;
  private copiedTimer: number | null = null;
  private draftOrder: LayerKey[] = [...CANONICAL_LAYER_ORDER];
  private draftVisibility: LayerVisibilityMap = { ...DEFAULT_LAYER_VISIBILITY };
  private draftShowTelemetryPublic = false;
  private draftDirty = false;
  private draggingKey: LayerKey | null = null;

  constructor(
    container: HTMLElement,
    initialState: AdminPanelState,
    callbacks: AdminPanelCallbacks
  ) {
    this.root = container;
    this.state = initialState;
    this.callbacks = callbacks;
    this.root.className = "tf-account-panel tf-admin-panel";
    this.root.setAttribute(
      "aria-label",
      "Admin Console — Traffic, Storage, Friend Invites and Users"
    );
    this.root.setAttribute("aria-hidden", "true");
    this.render();
  }

  isPanelOpen(): boolean {
    return this.isOpen;
  }

  open(tab?: AdminPanelTab): void {
    if (tab) this.activeTab = tab;
    this.isOpen = true;
    this.root.setAttribute("aria-hidden", "false");
    this.render();
  }

  close(): void {
    this.isOpen = false;
    this.root.setAttribute("aria-hidden", "true");
  }

  updateState(patch: Partial<AdminPanelState>): void {
    const incomingUiSettings = patch.uiSettings;
    this.state = { ...this.state, ...patch };
    if (incomingUiSettings && (!this.draftDirty || patch.busyAction === null)) {
      this.syncDraftFromSettings(incomingUiSettings);
    }
    this.render();
  }

  private syncDraftFromSettings(settings: AdminUiSettingsResponse): void {
    this.draftOrder = normalizeLayerOrder(settings.default_layer_order);
    this.draftVisibility = normalizeLayerVisibility(
      settings.default_layer_visibility
    );
    this.draftShowTelemetryPublic = Boolean(
      settings.show_stack_telemetry_public
    );
    this.draftDirty = false;
  }

  private render(): void {
    const activeInviteCount = this.state.invites.filter(
      (i) => classifyInviteStatus(i).status === "active"
    ).length;
    const userCount = this.state.users.length;
    const modeLabel = (
      this.state.stats?.registration_mode ?? this.state.registrationMode
    )
      .replace(/_/g, " ")
      .toUpperCase();

    this.root.innerHTML = `
      <div class="tf-inspector-head">
        <div>
          <div class="tf-inspector-kicker">Instance Administration · ${escapeHtml(modeLabel)}</div>
          <h2 class="tf-inspector-title">Admin Console</h2>
        </div>
        <div class="tf-head-actions">
          <button
            type="button"
            class="tf-mini-btn"
            data-role="admin-refresh"
            title="Refresh traffic, invites & users"
            ${this.state.loading ? "disabled" : ""}
          >↻</button>
          <button type="button" class="tf-inspector-close" data-role="close" aria-label="Close Admin Console">×</button>
        </div>
      </div>

      <div class="tf-panel-tabs tf-panel-tabs-4" role="tablist">
        <button
          type="button"
          class="tf-tab-btn ${this.activeTab === "overview" ? "active" : ""}"
          data-admin-tab="overview"
          role="tab"
          aria-selected="${this.activeTab === "overview"}"
        >Overview</button>
        <button
          type="button"
          class="tf-tab-btn ${this.activeTab === "invites" ? "active" : ""}"
          data-admin-tab="invites"
          role="tab"
          aria-selected="${this.activeTab === "invites"}"
        >Invites${activeInviteCount > 0 ? ` (${activeInviteCount})` : ""}</button>
        <button
          type="button"
          class="tf-tab-btn ${this.activeTab === "users" ? "active" : ""}"
          data-admin-tab="users"
          role="tab"
          aria-selected="${this.activeTab === "users"}"
        >Users${userCount > 0 ? ` (${userCount})` : ""}</button>
        <button
          type="button"
          class="tf-tab-btn ${this.activeTab === "settings" ? "active" : ""}"
          data-admin-tab="settings"
          role="tab"
          aria-selected="${this.activeTab === "settings"}"
        >Settings</button>
      </div>

      <div class="tf-inspector-body">
        ${
          this.state.error
            ? `<div class="tf-inspector-error">${escapeHtml(this.state.error)}</div>`
            : ""
        }
        ${
          this.activeTab === "overview"
            ? this.renderOverviewTab()
            : this.activeTab === "invites"
              ? this.renderInvitesTab()
              : this.activeTab === "users"
                ? this.renderUsersTab()
                : this.renderSettingsTab()
        }
      </div>
    `;

    this.bindEvents();
  }

  private renderOverviewTab(): string {
    const { stats, loading, busyAction } = this.state;

    if (!stats && loading) {
      return `<p class="tf-inspector-loading">Loading instance telemetry &amp; storage metrics…</p>`;
    }

    const t24 = normalizeTrafficWindow(stats?.traffic_24h);
    const t7d = normalizeTrafficWindow(stats?.traffic_7d);
    const routes = normalizeRouteBreakdown(stats?.route_breakdown).slice(0, 8);

    const storage = stats?.storage;
    const quota = computeStorageQuotaSummary(
      storage?.uploads_bytes,
      storage?.uploads_quota_bytes,
      storage?.uploads_usage_pct
    );

    const users = stats?.users;
    const invites = stats?.invites;
    const content = stats?.content;

    const routesHtml =
      routes.length > 0
        ? `
          <div class="tf-admin-table-wrap">
            <table class="tf-admin-table">
              <thead>
                <tr>
                  <th>Route Group</th>
                  <th class="num">Reqs</th>
                  <th class="num">Errs</th>
                  <th class="num">429s</th>
                </tr>
              </thead>
              <tbody>
                ${routes
                  .map(
                    (row) => `
                      <tr>
                        <td class="mono">${escapeHtml(row.routeGroup)}</td>
                        <td class="num mono">${escapeHtml(formatCompactCount(row.requests))}</td>
                        <td class="num mono ${row.errors > 0 ? "tf-admin-warn-val" : ""}">${escapeHtml(formatCompactCount(row.errors))}</td>
                        <td class="num mono ${row.rateLimited > 0 ? "tf-admin-crit-val" : ""}">${escapeHtml(formatCompactCount(row.rateLimited))}</td>
                      </tr>
                    `
                  )
                  .join("")}
              </tbody>
            </table>
          </div>
        `
        : `<p class="tf-inspector-muted">No API traffic buckets recorded yet.</p>`;

    return `
      <section class="tf-insp-section">
        <div class="tf-insp-sec-head">
          <h3>Traffic Overview</h3>
          <span class="mono tf-insp-meta">Rolling 24h / 7d</span>
        </div>

        <div class="tf-admin-kpi-grid">
          <div class="tf-stat-card">
            <span class="tf-stat-label">24h Requests</span>
            <span class="tf-stat-val mono">${escapeHtml(formatCompactCount(t24.requests))}</span>
            <span class="tf-admin-kpi-sub mono">${escapeHtml(formatStorageBytes(t24.bytesOut))} out</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">7d Requests</span>
            <span class="tf-stat-val mono">${escapeHtml(formatCompactCount(t7d.requests))}</span>
            <span class="tf-admin-kpi-sub mono">${escapeHtml(formatStorageBytes(t7d.bytesOut))} out</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">24h Errors / 7d</span>
            <span class="tf-stat-val mono ${t24.errors > 0 ? "tf-admin-warn-val" : ""}">${escapeHtml(formatCompactCount(t24.errors))} <span class="tf-admin-kpi-dim">/ ${escapeHtml(formatCompactCount(t7d.errors))}</span></span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">429 Rate Limited</span>
            <span class="tf-stat-val mono ${t24.rateLimited > 0 ? "tf-admin-crit-val" : ""}">${escapeHtml(formatCompactCount(t24.rateLimited))} <span class="tf-admin-kpi-dim">/ ${escapeHtml(formatCompactCount(t7d.rateLimited))}</span></span>
          </div>
        </div>

        <div class="tf-subhead">Top Route Groups</div>
        ${routesHtml}
      </section>

      <section class="tf-insp-section">
        <div class="tf-insp-sec-head">
          <h3>Storage &amp; Database</h3>
          <span class="mono tf-insp-meta">PostGIS + /data</span>
        </div>

        <div class="tf-kv-grid">
          <span class="tf-kv-key">PostGIS DB Size</span>
          <span class="tf-kv-val mono">${escapeHtml(formatStorageBytes(storage?.db_size_bytes))}</span>
          <span class="tf-kv-key">/data/tiles</span>
          <span class="tf-kv-val mono">${escapeHtml(formatStorageBytes(storage?.tiles_bytes))}</span>
          <span class="tf-kv-key">/data/packs</span>
          <span class="tf-kv-val mono">${escapeHtml(formatStorageBytes(storage?.packs_bytes))}</span>
          <span class="tf-kv-key">/data/uploads</span>
          <span class="tf-kv-val mono">${escapeHtml(quota.usedLabel)} / ${escapeHtml(quota.quotaLabel)}</span>
        </div>

        <div class="tf-quota-block">
          <div class="tf-quota-head">
            <span>Uploads Disk Quota</span>
            <span class="mono tf-quota-pct tone-${quota.tone}">${escapeHtml(quota.pctLabel)}</span>
          </div>
          <div class="tf-factor-track" role="progressbar" aria-valuenow="${Math.round(quota.clampedPct)}" aria-valuemin="0" aria-valuemax="100">
            <div class="tf-factor-fill tf-quota-fill tone-${quota.tone}" style="width:${quota.clampedPct}%"></div>
          </div>
        </div>
      </section>

      <section class="tf-insp-section">
        <div class="tf-insp-sec-head">
          <h3>Instance Inventory</h3>
          <span class="mono tf-insp-meta">${users ? `${users.active}/${users.total} active` : "Live"}</span>
        </div>
        <div class="tf-stat-grid">
          <div class="tf-stat-card">
            <span class="tf-stat-label">Tracks</span>
            <span class="tf-stat-val mono">${escapeHtml(formatCompactCount(content?.tracks))}</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Segments</span>
            <span class="tf-stat-val mono">${escapeHtml(formatCompactCount(content?.segments))}</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Trips</span>
            <span class="tf-stat-val mono">${escapeHtml(formatCompactCount(content?.trips))}</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Reports</span>
            <span class="tf-stat-val mono">${escapeHtml(formatCompactCount(content?.condition_reports))}</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Accounts</span>
            <span class="tf-stat-val mono">${escapeHtml(formatCompactCount(users?.total))}</span>
          </div>
          <div class="tf-stat-card">
            <span class="tf-stat-label">Active Invites</span>
            <span class="tf-stat-val mono">${escapeHtml(formatCompactCount(invites?.active))}</span>
          </div>
        </div>
      </section>

      <section class="tf-insp-section">
        <div class="tf-insp-sec-head">
          <h3>Admin Compute Actions</h3>
          <span class="mono tf-insp-meta">Admin Only</span>
        </div>
        <p class="tf-inspector-muted">Trigger official agency closure feed ingestion or recompute crowdsourced segment grades on demand.</p>
        <div class="tf-btn-row">
          <button
            type="button"
            class="tf-btn-secondary"
            data-role="sync-closures"
            ${busyAction !== null ? "disabled" : ""}
          >${busyAction === "sync-closures" ? "Syncing…" : "↻ Sync Closures"}</button>
          <button
            type="button"
            class="tf-btn-secondary"
            data-role="recompute-grades"
            ${busyAction !== null ? "disabled" : ""}
          >${busyAction === "recompute-grades" ? "Computing…" : "⚡ Recompute Live Grades"}</button>
        </div>
      </section>
    `;
  }

  private renderInvitesTab(): string {
    const { invites, latestCreatedInvite, busyAction } = this.state;

    const createdBanner = latestCreatedInvite
      ? `
        <div class="tf-match-summary">
          <div class="tf-insp-sec-head">
            <h3>New Invite Ready</h3>
            <span class="tf-mini-tag emerald mono">${escapeHtml(latestCreatedInvite.role.toUpperCase())}</span>
          </div>
          <div class="tf-admin-code-row">
            <code class="tf-admin-code mono">${escapeHtml(latestCreatedInvite.code)}</code>
            <button
              type="button"
              class="tf-mini-btn ${this.copiedCode === latestCreatedInvite.code ? "active" : ""}"
              data-copy-code="${escapeHtml(latestCreatedInvite.code)}"
            >${this.copiedCode === latestCreatedInvite.code ? "✓ Copied" : "Copy Code"}</button>
          </div>
          ${
            latestCreatedInvite.label
              ? `<div class="tf-list-card-sub">${escapeHtml(latestCreatedInvite.label)} · max ${latestCreatedInvite.max_uses} use(s)</div>`
              : ""
          }
        </div>
      `
      : "";

    const listHtml =
      invites.length > 0
        ? invites
            .map((inv) => {
              const badge = classifyInviteStatus(inv);
              const tagClass =
                badge.tone === "emerald"
                  ? "emerald"
                  : badge.tone === "amber"
                    ? "amber"
                    : badge.tone === "red"
                      ? "red"
                      : "";
              const isCopied = this.copiedCode === inv.code;
              const isRevoking = busyAction === `revoke-${inv.id}`;
              return `
                <div class="tf-list-card">
                  <div class="tf-list-card-head">
                    <span class="tf-admin-code-inline mono">${escapeHtml(inv.code)}</span>
                    <div class="tf-pack-badges">
                      <span class="tf-mini-tag ${inv.role === "admin" ? "amber" : "cyan"}">${escapeHtml(inv.role.toUpperCase())}</span>
                      <span class="tf-mini-tag ${tagClass}">${escapeHtml(badge.label.toUpperCase())}</span>
                    </div>
                  </div>
                  <div class="tf-list-card-sub">
                    ${inv.label ? `${escapeHtml(inv.label)} · ` : ""}<span class="mono">${inv.use_count}/${inv.max_uses} uses</span>
                    ${inv.expires_at ? ` · exp ${escapeHtml(formatShortTimestamp(inv.expires_at))}` : " · no expiry"}
                  </div>
                  <div class="tf-vehicle-actions">
                    <button
                      type="button"
                      class="tf-mini-btn ${isCopied ? "active" : ""}"
                      data-copy-code="${escapeHtml(inv.code)}"
                    >${isCopied ? "✓ Copied" : "Copy Code"}</button>
                    ${
                      badge.canRevoke
                        ? `<button type="button" class="tf-mini-btn danger" data-revoke-invite="${escapeHtml(inv.id)}" ${busyAction !== null ? "disabled" : ""}>${isRevoking ? "Revoking…" : "Revoke"}</button>`
                        : `<span class="tf-list-card-sub mono">Revoked ${escapeHtml(formatShortTimestamp(inv.revoked_at))}</span>`
                    }
                  </div>
                </div>
              `;
            })
            .join("")
        : `<p class="tf-inspector-muted">No invite codes created yet. Generate one above to share with trusted 4WD mates.</p>`;

    return `
      <section class="tf-insp-section">
        <div class="tf-insp-sec-head">
          <h3>Create Friend Invite</h3>
          <span class="mono tf-insp-meta">TF-XXXX-XXXX</span>
        </div>

        <form class="tf-form" data-role="create-invite-form">
          <label class="tf-field">
            <span class="tf-label">Label (optional)</span>
            <input type="text" class="tf-input" name="label" maxlength="120" placeholder="e.g. High Country Crew" />
          </label>

          <div class="tf-field-row">
            <label class="tf-field">
              <span class="tf-label">Max Uses</span>
              <input type="number" class="tf-input mono" name="max_uses" min="1" max="500" value="5" required />
            </label>
            <label class="tf-field">
              <span class="tf-label">Expires (Days)</span>
              <input type="number" class="tf-input mono" name="expires_in_days" min="1" max="365" value="30" placeholder="Blank = never" />
            </label>
          </div>

          <label class="tf-field">
            <span class="tf-label">Assigned Role</span>
            <select class="tf-select" name="role">
              <option value="member" selected>Member (Trips, Reports, Routes &amp; Pins)</option>
              <option value="admin">Admin (Full Console &amp; Compute)</option>
            </select>
          </label>

          <button type="submit" class="tf-btn-primary" ${busyAction !== null ? "disabled" : ""}>
            ${busyAction === "create-invite" ? "Generating…" : "+ Generate Invite Code"}
          </button>
        </form>

        ${createdBanner}
      </section>

      <section class="tf-insp-section">
        <div class="tf-insp-sec-head">
          <h3>Invite Codes</h3>
          <span class="mono tf-insp-meta">${invites.length} total</span>
        </div>
        <div class="tf-card-stack">${listHtml}</div>
      </section>
    `;
  }

  private renderUsersTab(): string {
    const { users, currentUserId, busyAction } = this.state;

    const listHtml =
      users.length > 0
        ? users
            .map((u) => {
              const isSelf = currentUserId !== null && u.id === currentUserId;
              const nextRole: UserRole = u.role === "admin" ? "member" : "admin";
              const nextActive = !u.is_active;
              const isBusyUser = busyAction === `user-${u.id}`;
              return `
                <div class="tf-list-card ${!u.is_active ? "tf-admin-user-suspended" : ""}">
                  <div class="tf-list-card-head">
                    <div>
                      <div class="tf-list-card-title">${escapeHtml(u.display_name)}${isSelf ? ` <span class="tf-mini-tag">YOU</span>` : ""}</div>
                      <div class="tf-list-card-sub mono">${escapeHtml(u.email)}</div>
                    </div>
                    <div class="tf-pack-badges">
                      <span class="tf-mini-tag ${u.role === "admin" ? "amber" : "cyan"}">${escapeHtml(u.role.toUpperCase())}</span>
                      <span class="tf-mini-tag ${u.is_active ? "emerald" : "red"}">${u.is_active ? "ACTIVE" : "SUSPENDED"}</span>
                    </div>
                  </div>
                  ${
                    u.invited_by_code || u.created_at
                      ? `
                        <div class="tf-list-card-tags">
                          ${
                            u.invited_by_code
                              ? `<span class="tf-mini-tag mono">Invite: ${escapeHtml(u.invited_by_code)}</span>`
                              : ""
                          }
                          ${
                            u.created_at
                              ? `<span class="tf-mini-tag mono">Joined ${escapeHtml(u.created_at.slice(0, 10))}</span>`
                              : ""
                          }
                        </div>
                      `
                      : ""
                  }
                  <div class="tf-vehicle-actions">
                    <button
                      type="button"
                      class="tf-mini-btn ${u.is_active ? "danger" : ""}"
                      data-toggle-active-user="${escapeHtml(u.id)}"
                      data-next-active="${String(nextActive)}"
                      ${busyAction !== null ? "disabled" : ""}
                    >${isBusyUser ? "Saving…" : u.is_active ? "Suspend" : "Re-enable"}</button>
                    <button
                      type="button"
                      class="tf-mini-btn"
                      data-toggle-role-user="${escapeHtml(u.id)}"
                      data-next-role="${nextRole}"
                      ${busyAction !== null ? "disabled" : ""}
                    >${nextRole === "admin" ? "Make Admin" : "Make Member"}</button>
                  </div>
                </div>
              `;
            })
            .join("")
        : `<p class="tf-inspector-muted">No registered user accounts found.</p>`;

    return `
      <section class="tf-insp-section">
        <div class="tf-insp-sec-head">
          <h3>Registered Accounts</h3>
          <span class="mono tf-insp-meta">${users.length} total</span>
        </div>
        <p class="tf-inspector-muted">Suspending a member immediately blocks existing tokens &amp; cookies with HTTP 403. The last active admin cannot be demoted or suspended.</p>
        <div class="tf-card-stack">${listHtml}</div>
      </section>
    `;
  }

  private renderSettingsTab(): string {
    const { uiSettings, loading, busyAction } = this.state;

    if (!uiSettings && loading) {
      return `<p class="tf-inspector-loading">Loading instance map &amp; UI defaults…</p>`;
    }

    const total = this.draftOrder.length;
    const envTelemetryDefault = uiSettings?.env_show_stack_telemetry_public
      ? "Visible"
      : "Hidden";
    const isSaving = busyAction === "save-ui-settings";
    const isResetting = busyAction === "reset-ui-settings";

    return `
      <section class="tf-insp-section">
        <div class="tf-insp-sec-head">
          <h3>Default Map Layers</h3>
          <span class="mono tf-insp-meta">${this.draftDirty ? "Unsaved" : "Synced"}</span>
        </div>
        <p class="tf-inspector-muted">Set the default sidebar order and initial ON/OFF state for visitors and users who haven&apos;t customized their own preferences.</p>

        <div class="tf-admin-layer-list" data-role="admin-layer-list">
          ${this.draftOrder
            .map((key, idx) => {
              const def = LAYER_DEFINITION_MAP[key];
              const enabled = Boolean(this.draftVisibility[key]);
              return `
                <div
                  class="tf-admin-layer-row"
                  data-admin-layer-row="${escapeHtml(key)}"
                  draggable="true"
                >
                  <span class="tf-layer-drag-handle" title="Drag to reorder" aria-hidden="true">⋮⋮</span>
                  <label class="tf-admin-layer-toggle">
                    <input
                      type="checkbox"
                      class="tf-checkbox"
                      data-admin-layer-vis="${escapeHtml(key)}"
                      ${enabled ? "checked" : ""}
                    />
                    <span class="tf-admin-layer-name">${escapeHtml(def.label)}</span>
                  </label>
                  <span class="tf-mini-tag mono ${enabled ? "emerald" : ""}">${enabled ? "ON" : "OFF"}</span>
                  <div class="tf-layer-move-controls always-visible">
                    <button
                      type="button"
                      class="tf-layer-move-btn"
                      data-admin-move-layer="${escapeHtml(key)}"
                      data-admin-move-dir="up"
                      title="Move ${escapeHtml(def.shortLabel)} up"
                      aria-label="Move ${escapeHtml(def.shortLabel)} up"
                      ${idx === 0 ? "disabled" : ""}
                    >↑</button>
                    <button
                      type="button"
                      class="tf-layer-move-btn"
                      data-admin-move-layer="${escapeHtml(key)}"
                      data-admin-move-dir="down"
                      title="Move ${escapeHtml(def.shortLabel)} down"
                      aria-label="Move ${escapeHtml(def.shortLabel)} down"
                      ${idx === total - 1 ? "disabled" : ""}
                    >↓</button>
                  </div>
                </div>
              `;
            })
            .join("")}
        </div>
      </section>

      <section class="tf-insp-section">
        <div class="tf-insp-sec-head">
          <h3>Sidebar Visibility</h3>
          <span class="mono tf-insp-meta">Public Visitors</span>
        </div>
        <label class="tf-toggle-row tf-admin-telemetry-toggle">
          <input
            type="checkbox"
            class="tf-checkbox"
            data-role="admin-telemetry-public"
            ${this.draftShowTelemetryPublic ? "checked" : ""}
          />
          <span class="tf-toggle-copy">
            <span class="tf-toggle-label">Show Stack &amp; Offline Telemetry publicly</span>
            <span class="tf-toggle-hint">Always visible to signed-in members and admins. Hoster env default: ${escapeHtml(envTelemetryDefault)} (<code class="mono">TRAILFORGE_SHOW_STACK_TELEMETRY_PUBLIC</code>).</span>
          </span>
        </label>
      </section>

      <div class="tf-btn-row">
        <button
          type="button"
          class="tf-btn-primary"
          data-role="save-ui-settings"
          ${busyAction !== null ? "disabled" : ""}
        >${isSaving ? "Saving…" : "Save Instance Defaults"}</button>
        <button
          type="button"
          class="tf-btn-secondary"
          data-role="reset-ui-settings"
          ${busyAction !== null ? "disabled" : ""}
        >${isResetting ? "Resetting…" : "Reset Factory"}</button>
      </div>
    `;
  }

  private bindEvents(): void {
    this.root
      .querySelector<HTMLButtonElement>("[data-role='close']")
      ?.addEventListener("click", () => {
        this.close();
        this.callbacks.onClose();
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='admin-refresh']")
      ?.addEventListener("click", () => {
        void this.callbacks.onRefresh();
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-admin-tab]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const next = btn.dataset.adminTab as AdminPanelTab | undefined;
          if (
            next === "overview" ||
            next === "invites" ||
            next === "users" ||
            next === "settings"
          ) {
            this.activeTab = next;
            this.render();
          }
        });
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='sync-closures']")
      ?.addEventListener("click", () => {
        void this.callbacks.onSyncClosures();
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='recompute-grades']")
      ?.addEventListener("click", () => {
        void this.callbacks.onRecomputeGrades();
      });

    this.root
      .querySelector<HTMLFormElement>("[data-role='create-invite-form']")
      ?.addEventListener("submit", (e) => {
        e.preventDefault();
        const fd = new FormData(e.currentTarget as HTMLFormElement);
        const label = String(fd.get("label") ?? "").trim() || null;
        const maxUsesRaw = Number(fd.get("max_uses") ?? 1);
        const max_uses = Number.isFinite(maxUsesRaw)
          ? Math.max(1, Math.min(500, Math.round(maxUsesRaw)))
          : 1;
        const expStr = String(fd.get("expires_in_days") ?? "").trim();
        const expNum = expStr ? Number(expStr) : null;
        const expires_in_days =
          expNum !== null && Number.isFinite(expNum) && expNum > 0
            ? Math.round(expNum)
            : null;
        const role =
          String(fd.get("role") ?? "member") === "admin" ? "admin" : "member";

        void this.callbacks
          .onCreateInvite({ label, role, max_uses, expires_in_days })
          .catch((err) => {
            this.updateState({ error: formatFriendlyApiError(err) });
          });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-copy-code]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const code = btn.dataset.copyCode;
          if (!code) return;
          void this.callbacks.onCopyInviteCode(code).then(() => {
            this.copiedCode = code;
            this.render();
            if (this.copiedTimer !== null) {
              window.clearTimeout(this.copiedTimer);
            }
            this.copiedTimer = window.setTimeout(() => {
              this.copiedCode = null;
              this.copiedTimer = null;
              if (this.isOpen) this.render();
            }, 2200);
          });
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-revoke-invite]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const id = btn.dataset.revokeInvite;
          if (id) void this.callbacks.onRevokeInvite(id);
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-toggle-active-user]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const id = btn.dataset.toggleActiveUser;
          const nextActive = btn.dataset.nextActive === "true";
          if (id) void this.callbacks.onPatchUser(id, { is_active: nextActive });
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-toggle-role-user]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const id = btn.dataset.toggleRoleUser;
          const nextRole =
            btn.dataset.nextRole === "admin" ? "admin" : "member";
          if (id) void this.callbacks.onPatchUser(id, { role: nextRole });
        });
      });

    // Settings tab bindings (default layer order, visibility & public stack telemetry)
    this.root
      .querySelectorAll<HTMLInputElement>("[data-admin-layer-vis]")
      .forEach((input) => {
        input.addEventListener("change", () => {
          const key = input.dataset.adminLayerVis as LayerKey | undefined;
          if (!key) return;
          this.draftVisibility = {
            ...this.draftVisibility,
            [key]: input.checked,
          };
          this.draftDirty = true;
          this.render();
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-admin-move-layer]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const key = btn.dataset.adminMoveLayer as LayerKey | undefined;
          const dir = btn.dataset.adminMoveDir as "up" | "down" | undefined;
          if (!key || (dir !== "up" && dir !== "down")) return;
          this.draftOrder = moveLayerInOrder(this.draftOrder, key, dir);
          this.draftDirty = true;
          this.render();
        });
      });

    const adminLayerList = this.root.querySelector<HTMLElement>(
      "[data-role='admin-layer-list']"
    );
    adminLayerList
      ?.querySelectorAll<HTMLElement>("[data-admin-layer-row]")
      .forEach((row) => {
        const key = row.dataset.adminLayerRow as LayerKey | undefined;
        if (!key) return;

        row.addEventListener("dragstart", (e) => {
          this.draggingKey = key;
          row.classList.add("dragging");
          if (e.dataTransfer) {
            e.dataTransfer.effectAllowed = "move";
            e.dataTransfer.setData("text/plain", key);
          }
        });

        row.addEventListener("dragover", (e) => {
          if (!this.draggingKey || this.draggingKey === key) return;
          e.preventDefault();
          if (e.dataTransfer) {
            e.dataTransfer.dropEffect = "move";
          }
          row.classList.add("drag-over");
        });

        row.addEventListener("dragleave", () => {
          row.classList.remove("drag-over");
        });

        row.addEventListener("drop", (e) => {
          e.preventDefault();
          row.classList.remove("drag-over");
          const src = this.draggingKey;
          this.draggingKey = null;
          if (!src || src === key) return;
          this.draftOrder = reorderLayerKeys(this.draftOrder, src, key);
          this.draftDirty = true;
          this.render();
        });

        row.addEventListener("dragend", () => {
          this.draggingKey = null;
          row.classList.remove("dragging");
          adminLayerList
            .querySelectorAll(".drag-over")
            .forEach((el) => el.classList.remove("drag-over"));
        });
      });

    this.root
      .querySelector<HTMLInputElement>("[data-role='admin-telemetry-public']")
      ?.addEventListener("change", (e) => {
        const target = e.currentTarget as HTMLInputElement;
        this.draftShowTelemetryPublic = target.checked;
        this.draftDirty = true;
        this.render();
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='save-ui-settings']")
      ?.addEventListener("click", () => {
        this.draftDirty = false;
        void this.callbacks.onSaveUiSettings({
          default_layer_order: [...this.draftOrder],
          default_layer_visibility: { ...this.draftVisibility },
          show_stack_telemetry_public: this.draftShowTelemetryPublic,
        });
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='reset-ui-settings']")
      ?.addEventListener("click", () => {
        this.draftDirty = false;
        void this.callbacks.onResetUiSettings();
      });
  }
}
