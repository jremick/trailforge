import { escapeHtml, getGradeColor } from "../presets";
import type {
  AuthUser,
  DrivetrainType,
  PinCategory,
  TireClassType,
  UserFavoriteItem,
  UserPinInput,
  UserPinItem,
  VehicleCapabilityClass,
  VehicleProfileInput,
  VehicleProfileItem,
} from "../types";
import {
  PIN_CATEGORY_OPTIONS,
  VEHICLE_CLASSES,
  VEHICLE_CLASS_META,
  classifyVehiclePreview,
  getPinCategoryColor,
  getVehicleClearanceMm,
  getVehicleLowRange,
  isVehicleActive,
} from "../vehicleHelpers";

export type AccountPanelTab = "rig" | "saved" | "account";

export interface AccountPanelState {
  user: AuthUser | null;
  anonymousRigClass: VehicleCapabilityClass;
  vehicles: VehicleProfileItem[];
  favorites: UserFavoriteItem[];
  pins: UserPinItem[];
  pinDropActive: boolean;
  pendingPinCoords: { lat: number; lon: number } | null;
}

export interface AccountPanelCallbacks {
  onClose: () => void;
  onSelectAnonymousRig: (cls: VehicleCapabilityClass) => void;
  onLogin: (email: string, password: string) => Promise<void>;
  onRegister: (
    displayName: string,
    email: string,
    password: string
  ) => Promise<void>;
  onLogout: () => Promise<void>;
  onSaveVehicle: (
    payload: VehicleProfileInput,
    editingId: string | null
  ) => Promise<void>;
  onActivateVehicle: (id: string) => Promise<void>;
  onDeleteVehicle: (id: string) => Promise<void>;
  onOpenFavoriteTrack: (trackId: string) => void;
  onRemoveFavorite: (trackId: string) => Promise<void>;
  onTogglePinDropMode: (active: boolean) => void;
  onCreatePin: (payload: UserPinInput) => Promise<void>;
  onSelectPin: (pin: UserPinItem) => void;
  onDeletePin: (id: string) => Promise<void>;
  onExportUser: (format: "gpx" | "geojson") => Promise<void>;
}

function defaultVehicleDraft(): VehicleProfileInput {
  return {
    name: "",
    drivetrain: "4wd_part_time",
    low_range: true,
    lift_mm: 50,
    clearance_mm: 240,
    tire_class: "AT",
    front_locker: false,
    rear_locker: false,
    has_winch: false,
    has_snorkel: false,
    is_active: true,
  };
}

export class RigAccountPanel {
  private readonly root: HTMLElement;
  private readonly callbacks: AccountPanelCallbacks;
  private isOpen = false;
  private activeTab: AccountPanelTab = "rig";
  private authMode: "login" | "register" = "login";
  private editingVehicleId: string | null = null;
  private showVehicleForm = false;
  private vehicleDraft: VehicleProfileInput = defaultVehicleDraft();
  private formError: string | null = null;
  private busy = false;
  private state: AccountPanelState;

  constructor(
    container: HTMLElement,
    initialState: AccountPanelState,
    callbacks: AccountPanelCallbacks
  ) {
    this.root = container;
    this.state = initialState;
    this.callbacks = callbacks;
    this.root.className = "tf-account-panel";
    this.root.setAttribute("aria-label", "Rig profiles, saved tracks, pins, and account");
    this.root.setAttribute("aria-hidden", "true");
    this.render();
  }

  open(tab?: AccountPanelTab): void {
    if (tab) this.activeTab = tab;
    this.isOpen = true;
    this.formError = null;
    this.root.setAttribute("aria-hidden", "false");
    this.render();
  }

  close(): void {
    this.isOpen = false;
    this.formError = null;
    this.root.setAttribute("aria-hidden", "true");
  }

  isPanelOpen(): boolean {
    return this.isOpen;
  }

  updateState(next: AccountPanelState): void {
    this.state = next;
    this.render();
  }

  private render(): void {
    const { user, favorites, pins } = this.state;
    const savedCount = favorites.length + pins.length;

    this.root.innerHTML = `
      <div class="tf-inspector-head">
        <div>
          <div class="tf-inspector-kicker">Rig &amp; Personal Data</div>
          <h2 class="tf-inspector-title">${
            user ? escapeHtml(user.display_name) : "Anonymous Mode"
          }</h2>
        </div>
        <button type="button" class="tf-inspector-close" data-role="close" aria-label="Close Rig & Account panel">×</button>
      </div>

      <div class="tf-panel-tabs" role="tablist">
        <button type="button" class="tf-tab-btn ${this.activeTab === "rig" ? "active" : ""}" data-tab="rig" role="tab" aria-selected="${this.activeTab === "rig"}">My Rig</button>
        <button type="button" class="tf-tab-btn ${this.activeTab === "saved" ? "active" : ""}" data-tab="saved" role="tab" aria-selected="${this.activeTab === "saved"}">Saved &amp; Pins${savedCount > 0 ? ` (${savedCount})` : ""}</button>
        <button type="button" class="tf-tab-btn ${this.activeTab === "account" ? "active" : ""}" data-tab="account" role="tab" aria-selected="${this.activeTab === "account"}">${user ? "Account" : "Sign In"}</button>
      </div>

      <div class="tf-inspector-body">
        ${
          this.formError
            ? `<div class="tf-inspector-error">${escapeHtml(this.formError)}</div>`
            : ""
        }
        ${
          this.activeTab === "rig"
            ? this.renderRigTab()
            : this.activeTab === "saved"
              ? this.renderSavedTab()
              : this.renderAccountTab()
        }
      </div>
    `;

    this.bindEvents();
  }

  private renderRigTab(): string {
    const { user, anonymousRigClass, vehicles } = this.state;

    if (!user) {
      return `
        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>Quick Rig Selector (No Account Needed)</h3>
            <span class="mono tf-insp-meta">${escapeHtml(anonymousRigClass)}</span>
          </div>
          <p class="tf-inspector-muted">Choose your rig class to see instant personalized suitability guidance ("Suited", "Challenging", "High Risk for your rig") on every track.</p>
          <div class="tf-rig-grid">
            ${VEHICLE_CLASSES.map((cls) => {
              const meta = VEHICLE_CLASS_META[cls];
              const active = cls === anonymousRigClass;
              return `
                <button type="button" class="tf-rig-card ${active ? "active" : ""}" data-anon-rig="${escapeHtml(cls)}">
                  <div class="tf-rig-card-head">
                    <span class="tf-rig-dot" style="background:${meta.color}"></span>
                    <span class="tf-rig-card-title">${escapeHtml(cls)}</span>
                  </div>
                  <span class="tf-rig-card-desc">${escapeHtml(meta.summary)}</span>
                </button>
              `;
            }).join("")}
          </div>
        </section>

        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>Custom Vehicle Profiles</h3>
            <span class="mono tf-insp-meta">Optional Account</span>
          </div>
          <p class="tf-inspector-muted">Sign in to build custom profiles with exact lift, ground clearance, tyres (HT/AT/MT), lockers, winch &amp; snorkel, persisted across sessions.</p>
          <button type="button" class="tf-btn-secondary" data-jump-tab="account">Sign In or Create Local Account</button>
        </section>
      `;
    }

    const preview = classifyVehiclePreview(this.vehicleDraft);
    const previewMeta = VEHICLE_CLASS_META[preview.capability_class];

    const profilesHtml =
      vehicles.length > 0
        ? vehicles
            .map((v) => {
              const active = isVehicleActive(v);
              const meta = VEHICLE_CLASS_META[v.capability_class];
              const specs = [
                v.drivetrain.replace(/_/g, " "),
                `${v.tire_class} tyres`,
                `${v.lift_mm}mm lift`,
                `${getVehicleClearanceMm(v)}mm clr`,
                v.front_locker || v.rear_locker ? "lockers" : null,
                v.has_winch ? "winch" : null,
                v.has_snorkel ? "snorkel" : null,
              ]
                .filter(Boolean)
                .join(" · ");
              return `
                <div class="tf-vehicle-card ${active ? "active" : ""}">
                  <div class="tf-vehicle-head">
                    <div>
                      <div class="tf-vehicle-name">${escapeHtml(v.name)}</div>
                      <div class="tf-vehicle-specs">${escapeHtml(specs)}</div>
                    </div>
                    <span class="tf-pill" style="border-color:${meta.color};color:${meta.color}">${escapeHtml(v.capability_class)}</span>
                  </div>
                  ${
                    v.classification_reasons && v.classification_reasons.length > 0
                      ? `<div class="tf-vehicle-reasons">${escapeHtml(v.classification_reasons.join(" · "))}</div>`
                      : ""
                  }
                  <div class="tf-vehicle-actions">
                    ${
                      active
                        ? `<span class="tf-active-badge">✓ Active Rig</span>`
                        : `<button type="button" class="tf-mini-btn" data-activate-vehicle="${escapeHtml(v.id)}">Set Active</button>`
                    }
                    <button type="button" class="tf-mini-btn" data-edit-vehicle="${escapeHtml(v.id)}">Edit</button>
                    <button type="button" class="tf-mini-btn danger" data-delete-vehicle="${escapeHtml(v.id)}">Delete</button>
                  </div>
                </div>
              `;
            })
            .join("")
        : `<p class="tf-inspector-muted">No vehicle profiles yet. Add your 4WD below to personalize track suitability.</p>`;

    const formHtml = this.showVehicleForm
      ? `
        <form class="tf-form" data-role="vehicle-form">
          <div class="tf-insp-sec-head">
            <h3>${this.editingVehicleId ? "Edit Vehicle Profile" : "New Vehicle Profile"}</h3>
            <span class="tf-pill" data-role="preview-class" style="border-color:${previewMeta.color};color:${previewMeta.color}">${escapeHtml(preview.capability_class)}</span>
          </div>
          <div class="tf-preview-reasons" data-role="preview-reasons">${escapeHtml(preview.reasons.join(" · "))}</div>

          <label class="tf-field">
            <span class="tf-label">Vehicle Name</span>
            <input type="text" class="tf-input" name="name" required maxlength="60" placeholder="e.g. 2019 Hilux SR5 Touring" value="${escapeHtml(this.vehicleDraft.name)}" />
          </label>

          <div class="tf-field-row">
            <label class="tf-field">
              <span class="tf-label">Drivetrain</span>
              <select class="tf-select" name="drivetrain">
                <option value="awd" ${this.vehicleDraft.drivetrain === "awd" ? "selected" : ""}>AWD</option>
                <option value="4wd_part_time" ${this.vehicleDraft.drivetrain === "4wd_part_time" ? "selected" : ""}>4WD Part-Time</option>
                <option value="4wd_full_time" ${this.vehicleDraft.drivetrain === "4wd_full_time" ? "selected" : ""}>4WD Full-Time</option>
              </select>
            </label>
            <label class="tf-field">
              <span class="tf-label">Tyre Class</span>
              <select class="tf-select" name="tire_class">
                <option value="HT" ${this.vehicleDraft.tire_class === "HT" ? "selected" : ""}>HT (Highway)</option>
                <option value="AT" ${this.vehicleDraft.tire_class === "AT" ? "selected" : ""}>AT (All-Terrain)</option>
                <option value="MT" ${this.vehicleDraft.tire_class === "MT" ? "selected" : ""}>MT (Mud-Terrain)</option>
              </select>
            </label>
          </div>

          <div class="tf-field-row">
            <label class="tf-field">
              <span class="tf-label">Suspension Lift (mm)</span>
              <input type="number" class="tf-input mono" name="lift_mm" min="0" max="300" step="5" value="${this.vehicleDraft.lift_mm}" />
            </label>
            <label class="tf-field">
              <span class="tf-label">Ground Clearance (mm)</span>
              <input type="number" class="tf-input mono" name="clearance_mm" min="120" max="500" step="5" value="${this.vehicleDraft.clearance_mm}" />
            </label>
          </div>

          <div class="tf-check-grid">
            <label class="tf-inline-check"><input type="checkbox" name="low_range" ${this.vehicleDraft.low_range ? "checked" : ""} /> Low-Range</label>
            <label class="tf-inline-check"><input type="checkbox" name="rear_locker" ${this.vehicleDraft.rear_locker ? "checked" : ""} /> Rear Locker</label>
            <label class="tf-inline-check"><input type="checkbox" name="front_locker" ${this.vehicleDraft.front_locker ? "checked" : ""} /> Front Locker</label>
            <label class="tf-inline-check"><input type="checkbox" name="has_winch" ${this.vehicleDraft.has_winch ? "checked" : ""} /> Winch</label>
            <label class="tf-inline-check"><input type="checkbox" name="has_snorkel" ${this.vehicleDraft.has_snorkel ? "checked" : ""} /> Snorkel</label>
          </div>

          <div class="tf-btn-row">
            <button type="submit" class="tf-btn-primary" ${this.busy ? "disabled" : ""}>${this.editingVehicleId ? "Update Rig" : "Save Vehicle Profile"}</button>
            <button type="button" class="tf-btn-secondary" data-role="cancel-vehicle">Cancel</button>
          </div>
        </form>
      `
      : `<button type="button" class="tf-btn-primary" data-role="new-vehicle">+ Add Vehicle Profile</button>`;

    return `
      <section class="tf-insp-section">
        <div class="tf-insp-sec-head">
          <h3>Saved Vehicle Profiles</h3>
          <span class="mono tf-insp-meta">${vehicles.length}</span>
        </div>
        <div class="tf-card-stack">${profilesHtml}</div>
        ${formHtml}
      </section>
    `;
  }

  private renderSavedTab(): string {
    const { user, favorites, pins, pinDropActive, pendingPinCoords } =
      this.state;

    if (!user) {
      return `
        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>My Favorites &amp; Personal Pins</h3>
            <span class="mono tf-insp-meta">Sign In Required</span>
          </div>
          <p class="tf-inspector-muted">Sign in to save favorite VIC/NSW tracks, drop personal camp/hazard/recovery pins on the map, and export your full trip pack as GPX or GeoJSON.</p>
          <p class="tf-inspector-muted">Note: Individual track GPX/GeoJSON exports work directly in the Track Inspector without an account.</p>
          <button type="button" class="tf-btn-primary" data-jump-tab="account">Sign In / Create Account</button>
        </section>
      `;
    }

    const favsHtml =
      favorites.length > 0
        ? favorites
            .map((fav) => {
              const title = fav.track_name ?? fav.name ?? fav.track_id;
              const grade = fav.grade ?? "";
              const dist =
                typeof fav.total_length_m === "number"
                  ? `${(fav.total_length_m / 1000).toFixed(1)} km`
                  : "";
              return `
                <div class="tf-saved-row">
                  <button type="button" class="tf-saved-main" data-open-fav="${escapeHtml(fav.track_id)}">
                    <span class="tf-saved-title">${escapeHtml(title)}</span>
                    <span class="tf-saved-sub">
                      ${grade ? `<span style="color:${getGradeColor(grade)}">${escapeHtml(grade)}</span>` : ""}
                      ${fav.state ? ` · ${escapeHtml(fav.state)}` : ""}
                      ${dist ? ` · ${escapeHtml(dist)}` : ""}
                    </span>
                  </button>
                  <button type="button" class="tf-mini-btn danger" data-remove-fav="${escapeHtml(fav.track_id)}" title="Remove favorite">×</button>
                </div>
              `;
            })
            .join("")
        : `<p class="tf-inspector-muted">No favorite tracks saved yet. Click ☆ Save inside any Track Inspector.</p>`;

    const pinsHtml =
      pins.length > 0
        ? pins
            .map((pin, idx) => {
              const name = pin.name ?? pin.title ?? "Pin";
              const color = getPinCategoryColor(pin.category);
              return `
                <div class="tf-saved-row">
                  <button type="button" class="tf-saved-main" data-goto-pin="${idx}">
                    <span class="tf-saved-title"><span class="tf-rig-dot" style="background:${color}"></span> ${escapeHtml(name)}</span>
                    <span class="tf-saved-sub mono">${escapeHtml(pin.category)} · ${pin.lat.toFixed(4)}, ${pin.lon.toFixed(4)}</span>
                    ${pin.notes ? `<span class="tf-saved-note">${escapeHtml(pin.notes)}</span>` : ""}
                  </button>
                  <button type="button" class="tf-mini-btn danger" data-delete-pin="${escapeHtml(pin.id)}" title="Delete pin">×</button>
                </div>
              `;
            })
            .join("")
        : `<p class="tf-inspector-muted">No personal pins dropped yet.</p>`;

    return `
      <section class="tf-insp-section">
        <div class="tf-insp-sec-head">
          <h3>Export Saved Pack</h3>
          <span class="mono tf-insp-meta">GridForge Ready</span>
        </div>
        <div class="tf-btn-row">
          <button type="button" class="tf-btn-secondary" data-export-user="gpx">⬇ Export All GPX</button>
          <button type="button" class="tf-btn-secondary" data-export-user="geojson">⬇ Export All GeoJSON</button>
        </div>
      </section>

      <section class="tf-insp-section">
        <div class="tf-insp-sec-head">
          <h3>Personal Map Pins</h3>
          <span class="mono tf-insp-meta">${pins.length}</span>
        </div>

        <button type="button" class="tf-btn-secondary ${pinDropActive ? "active-drop" : ""}" data-role="toggle-pin-drop">
          ${pinDropActive ? "⌖ Click Map to Pick Coords (Active) — Cancel" : "⌖ Drop Pin on Map"}
        </button>

        ${
          pendingPinCoords
            ? `
              <form class="tf-form" data-role="pin-form">
                <div class="tf-pin-coords mono">Selected: ${pendingPinCoords.lat.toFixed(5)}, ${pendingPinCoords.lon.toFixed(5)}</div>
                <label class="tf-field">
                  <span class="tf-label">Pin Name</span>
                  <input type="text" class="tf-input" name="name" required maxlength="80" placeholder="e.g. Creek Camp / Fallen Tree" />
                </label>
                <label class="tf-field">
                  <span class="tf-label">Category</span>
                  <select class="tf-select" name="category">
                    ${PIN_CATEGORY_OPTIONS.map(
                      (c) =>
                        `<option value="${escapeHtml(c.value)}">${escapeHtml(c.label)}</option>`
                    ).join("")}
                  </select>
                </label>
                <label class="tf-field">
                  <span class="tf-label">Notes (optional)</span>
                  <input type="text" class="tf-input" name="notes" maxlength="200" placeholder="Flat camp for 3 rigs, water nearby" />
                </label>
                <div class="tf-btn-row">
                  <button type="submit" class="tf-btn-primary" ${this.busy ? "disabled" : ""}>Save Pin</button>
                  <button type="button" class="tf-btn-secondary" data-role="cancel-pin">Cancel</button>
                </div>
              </form>
            `
            : ""
        }

        <div class="tf-card-stack">${pinsHtml}</div>
      </section>

      <section class="tf-insp-section">
        <div class="tf-insp-sec-head">
          <h3>Favorite Tracks</h3>
          <span class="mono tf-insp-meta">${favorites.length}</span>
        </div>
        <div class="tf-card-stack">${favsHtml}</div>
      </section>
    `;
  }

  private renderAccountTab(): string {
    const { user, vehicles, favorites, pins } = this.state;

    if (user) {
      const activeRig = vehicles.find((v) => isVehicleActive(v));
      return `
        <section class="tf-insp-section">
          <div class="tf-insp-sec-head">
            <h3>Signed In</h3>
            <span class="mono tf-insp-meta">Self-Hosted Auth</span>
          </div>
          <div class="tf-kv-grid">
            <span class="tf-kv-key">Name</span>
            <span class="tf-kv-val">${escapeHtml(user.display_name)}</span>
            <span class="tf-kv-key">Email</span>
            <span class="tf-kv-val mono">${escapeHtml(user.email)}</span>
            <span class="tf-kv-key">Active Rig</span>
            <span class="tf-kv-val">${escapeHtml(activeRig ? `${activeRig.name} (${activeRig.capability_class})` : "None set")}</span>
            <span class="tf-kv-key">Saved</span>
            <span class="tf-kv-val mono">${favorites.length} tracks · ${pins.length} pins</span>
          </div>
          <button type="button" class="tf-btn-secondary danger-outline" data-role="logout-btn" ${this.busy ? "disabled" : ""}>Sign Out</button>
        </section>
      `;
    }

    return `
      <section class="tf-insp-section">
        <div class="tf-auth-switch">
          <button type="button" class="tf-mini-btn ${this.authMode === "login" ? "active" : ""}" data-auth-mode="login">Sign In</button>
          <button type="button" class="tf-mini-btn ${this.authMode === "register" ? "active" : ""}" data-auth-mode="register">Create Account</button>
        </div>

        <form class="tf-form" data-role="auth-form">
          ${
            this.authMode === "register"
              ? `
                <label class="tf-field">
                  <span class="tf-label">Display Name</span>
                  <input type="text" class="tf-input" name="display_name" required maxlength="60" placeholder="e.g. High Country Driver" />
                </label>
              `
              : ""
          }
          <label class="tf-field">
            <span class="tf-label">Email</span>
            <input type="email" class="tf-input" name="email" required placeholder="driver@trailforge.local" />
          </label>
          <label class="tf-field">
            <span class="tf-label">Password</span>
            <input type="password" class="tf-input" name="password" required minlength="8" placeholder="Min 8 characters" />
          </label>
          <button type="submit" class="tf-btn-primary" ${this.busy ? "disabled" : ""}>
            ${this.authMode === "register" ? "Create Local Account" : "Sign In"}
          </button>
        </form>
        <p class="tf-inspector-muted">Accounts live purely in your self-hosted TrailForge PostGIS instance. Anonymous browsing always remains available.</p>
      </section>
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
      .querySelectorAll<HTMLButtonElement>("[data-tab]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          this.activeTab = btn.dataset.tab as AccountPanelTab;
          this.formError = null;
          this.render();
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-jump-tab]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          this.activeTab = btn.dataset.jumpTab as AccountPanelTab;
          this.formError = null;
          this.render();
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-anon-rig]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const cls = btn.dataset.anonRig as VehicleCapabilityClass;
          this.callbacks.onSelectAnonymousRig(cls);
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-auth-mode]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          this.authMode = btn.dataset.authMode as "login" | "register";
          this.formError = null;
          this.render();
        });
      });

    this.root
      .querySelector<HTMLFormElement>("[data-role='auth-form']")
      ?.addEventListener("submit", (e) => {
        e.preventDefault();
        const form = e.currentTarget as HTMLFormElement;
        const fd = new FormData(form);
        const email = String(fd.get("email") ?? "").trim();
        const password = String(fd.get("password") ?? "");
        const displayName = String(fd.get("display_name") ?? "").trim();

        void this.runAsync(async () => {
          if (this.authMode === "register") {
            await this.callbacks.onRegister(displayName, email, password);
          } else {
            await this.callbacks.onLogin(email, password);
          }
          this.activeTab = "rig";
        });
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='logout-btn']")
      ?.addEventListener("click", () => {
        void this.runAsync(async () => {
          await this.callbacks.onLogout();
          this.showVehicleForm = false;
          this.editingVehicleId = null;
        });
      });

    // Vehicle Profile actions
    this.root
      .querySelector<HTMLButtonElement>("[data-role='new-vehicle']")
      ?.addEventListener("click", () => {
        this.editingVehicleId = null;
        this.vehicleDraft = defaultVehicleDraft();
        this.showVehicleForm = true;
        this.formError = null;
        this.render();
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='cancel-vehicle']")
      ?.addEventListener("click", () => {
        this.showVehicleForm = false;
        this.editingVehicleId = null;
        this.formError = null;
        this.render();
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-edit-vehicle]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const id = btn.dataset.editVehicle!;
          const existing = this.state.vehicles.find((v) => v.id === id);
          if (!existing) return;
          this.editingVehicleId = existing.id;
          this.vehicleDraft = {
            name: existing.name,
            drivetrain: existing.drivetrain,
            low_range: getVehicleLowRange(existing),
            lift_mm: existing.lift_mm,
            clearance_mm: getVehicleClearanceMm(existing),
            tire_class: existing.tire_class,
            front_locker: existing.front_locker,
            rear_locker: existing.rear_locker,
            has_winch: existing.has_winch,
            has_snorkel: existing.has_snorkel,
            is_active: isVehicleActive(existing),
          };
          this.showVehicleForm = true;
          this.formError = null;
          this.render();
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-activate-vehicle]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const id = btn.dataset.activateVehicle!;
          void this.runAsync(() => this.callbacks.onActivateVehicle(id));
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-delete-vehicle]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const id = btn.dataset.deleteVehicle!;
          void this.runAsync(() => this.callbacks.onDeleteVehicle(id));
        });
      });

    const vehicleForm = this.root.querySelector<HTMLFormElement>(
      "[data-role='vehicle-form']"
    );
    if (vehicleForm) {
      const readDraft = (): VehicleProfileInput => {
        const fd = new FormData(vehicleForm);
        const drivetrain = String(
          fd.get("drivetrain") ?? "4wd_part_time"
        ) as DrivetrainType;
        return {
          name: String(fd.get("name") ?? "").trim(),
          drivetrain,
          low_range: fd.get("low_range") === "on",
          lift_mm: Math.max(0, Math.min(300, Number(fd.get("lift_mm") ?? 0))),
          clearance_mm: Math.max(
            120,
            Math.min(500, Number(fd.get("clearance_mm") ?? 210))
          ),
          tire_class: String(fd.get("tire_class") ?? "AT") as TireClassType,
          front_locker: fd.get("front_locker") === "on",
          rear_locker: fd.get("rear_locker") === "on",
          has_winch: fd.get("has_winch") === "on",
          has_snorkel: fd.get("has_snorkel") === "on",
          is_active: this.editingVehicleId ? this.vehicleDraft.is_active : true,
        };
      };

      vehicleForm.addEventListener("input", () => {
        this.vehicleDraft = readDraft();
        const preview = classifyVehiclePreview(this.vehicleDraft);
        const meta = VEHICLE_CLASS_META[preview.capability_class];
        const pill = vehicleForm.querySelector<HTMLElement>(
          "[data-role='preview-class']"
        );
        const reasons = vehicleForm.querySelector<HTMLElement>(
          "[data-role='preview-reasons']"
        );
        if (pill) {
          pill.textContent = preview.capability_class;
          pill.style.borderColor = meta.color;
          pill.style.color = meta.color;
        }
        if (reasons) {
          reasons.textContent = preview.reasons.join(" · ");
        }
      });

      vehicleForm.addEventListener("submit", (e) => {
        e.preventDefault();
        const payload = readDraft();
        const editId = this.editingVehicleId;
        void this.runAsync(async () => {
          await this.callbacks.onSaveVehicle(payload, editId);
          this.showVehicleForm = false;
          this.editingVehicleId = null;
          this.vehicleDraft = defaultVehicleDraft();
        });
      });
    }

    // Favorites & Pins actions
    this.root
      .querySelectorAll<HTMLButtonElement>("[data-open-fav]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          this.callbacks.onOpenFavoriteTrack(btn.dataset.openFav!);
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-remove-fav]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const trackId = btn.dataset.removeFav!;
          void this.runAsync(() => this.callbacks.onRemoveFavorite(trackId));
        });
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='toggle-pin-drop']")
      ?.addEventListener("click", () => {
        this.callbacks.onTogglePinDropMode(!this.state.pinDropActive);
      });

    this.root
      .querySelector<HTMLButtonElement>("[data-role='cancel-pin']")
      ?.addEventListener("click", () => {
        this.callbacks.onTogglePinDropMode(false);
      });

    this.root
      .querySelector<HTMLFormElement>("[data-role='pin-form']")
      ?.addEventListener("submit", (e) => {
        e.preventDefault();
        const coords = this.state.pendingPinCoords;
        if (!coords) return;
        const fd = new FormData(e.currentTarget as HTMLFormElement);
        const payload: UserPinInput = {
          name: String(fd.get("name") ?? "").trim(),
          category: String(fd.get("category") ?? "waypoint") as PinCategory,
          notes: String(fd.get("notes") ?? "").trim() || null,
          lat: coords.lat,
          lon: coords.lon,
        };
        void this.runAsync(() => this.callbacks.onCreatePin(payload));
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-goto-pin]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const pin = this.state.pins[Number(btn.dataset.gotoPin)];
          if (pin) this.callbacks.onSelectPin(pin);
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-delete-pin]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const id = btn.dataset.deletePin!;
          void this.runAsync(() => this.callbacks.onDeletePin(id));
        });
      });

    this.root
      .querySelectorAll<HTMLButtonElement>("[data-export-user]")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const fmt = btn.dataset.exportUser as "gpx" | "geojson";
          void this.runAsync(() => this.callbacks.onExportUser(fmt));
        });
      });
  }

  private async runAsync(task: () => Promise<void>): Promise<void> {
    this.busy = true;
    this.formError = null;
    this.render();
    try {
      await task();
    } catch (err) {
      this.formError =
        err instanceof Error ? err.message : "Operation failed";
    } finally {
      this.busy = false;
      this.render();
    }
  }
}
