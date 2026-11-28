export type SheetSnapPoint = "peek" | "half" | "full";

export const MOBILE_SHEET_BREAKPOINT_PX = 768;

export const SHEET_SNAP_RATIOS: Readonly<Record<SheetSnapPoint, number>> = {
  peek: 0.34,
  half: 0.58,
  full: 0.92,
};

export const SHEET_SNAP_ORDER: readonly SheetSnapPoint[] = [
  "peek",
  "half",
  "full",
];

export function isMobileSheetViewport(viewportWidth?: number): boolean {
  const w =
    viewportWidth ??
    (typeof window !== "undefined" ? window.innerWidth : 1280);
  return Number.isFinite(w) && w <= MOBILE_SHEET_BREAKPOINT_PX;
}

export function computeSheetSnapHeightPx(
  snap: SheetSnapPoint,
  containerHeightPx: number
): number {
  const safeH = Math.max(240, Math.round(containerHeightPx || 600));
  const minPeek = Math.min(220, Math.round(safeH * 0.42));
  const raw = Math.round(safeH * SHEET_SNAP_RATIOS[snap]);
  if (snap === "peek") return Math.max(minPeek, raw);
  if (snap === "full") return Math.min(safeH - 8, Math.max(360, raw));
  return Math.max(minPeek + 80, Math.min(safeH - 48, raw));
}

export function resolveSheetSnapAfterDrag(params: {
  currentHeightPx: number;
  containerHeightPx: number;
  velocityPxPerMs: number;
  startSnap: SheetSnapPoint;
}): SheetSnapPoint | "dismiss" {
  const { currentHeightPx, containerHeightPx, velocityPxPerMs, startSnap } =
    params;
  const peekH = computeSheetSnapHeightPx("peek", containerHeightPx);
  const halfH = computeSheetSnapHeightPx("half", containerHeightPx);
  const fullH = computeSheetSnapHeightPx("full", containerHeightPx);

  // Fast downward flick dismisses when already at peek or dragged past dismiss threshold
  if (velocityPxPerMs > 0.65) {
    if (startSnap === "full" && currentHeightPx > halfH - 40) return "half";
    if (startSnap === "half" && currentHeightPx > peekH - 30) return "peek";
    return "dismiss";
  }

  // Fast upward flick steps up one snap point
  if (velocityPxPerMs < -0.55) {
    if (startSnap === "peek") return "half";
    return "full";
  }

  // Dismiss if dragged well below peek height
  if (currentHeightPx < Math.round(peekH * 0.62)) {
    return "dismiss";
  }

  const candidates: Array<{ snap: SheetSnapPoint; dist: number }> = [
    { snap: "peek", dist: Math.abs(currentHeightPx - peekH) },
    { snap: "half", dist: Math.abs(currentHeightPx - halfH) },
    { snap: "full", dist: Math.abs(currentHeightPx - fullH) },
  ];
  candidates.sort((a, b) => a.dist - b.dist);
  return candidates[0]!.snap;
}

export function cycleSheetSnapPoint(current: SheetSnapPoint): SheetSnapPoint {
  if (current === "peek") return "half";
  if (current === "half") return "full";
  return "peek";
}

export interface SheetAttachOptions {
  onDismiss: () => void;
  initialSnap?: SheetSnapPoint;
}

/**
 * Attaches mobile draggable bottom-sheet snap behavior to any right-hand drawer
 * (`<aside>`). Uses event delegation so drawers that re-render via `innerHTML`
 * automatically preserve smooth drag, tap-to-cycle, and keyboard snap controls.
 */
export class MobileBottomSheetController {
  private readonly drawerEl: HTMLElement;
  private readonly onDismiss: () => void;
  private snap: SheetSnapPoint;
  private activePointerId: number | null = null;
  private startY = 0;
  private startHeight = 0;
  private lastY = 0;
  private lastTs = 0;
  private velocity = 0;
  private moved = false;

  constructor(drawerEl: HTMLElement, options: SheetAttachOptions) {
    this.drawerEl = drawerEl;
    this.onDismiss = options.onDismiss;
    this.snap = options.initialSnap ?? "half";
    this.drawerEl.dataset.sheetSnap = this.snap;
    this.bindEvents();
  }

  setSnap(next: SheetSnapPoint): void {
    this.snap = next;
    this.drawerEl.dataset.sheetSnap = next;
    this.drawerEl.style.removeProperty("--sheet-drag-h");
    this.drawerEl.classList.remove("sheet-dragging");
  }

  reset(initialSnap: SheetSnapPoint = "half"): void {
    this.activePointerId = null;
    this.setSnap(initialSnap);
  }

  private getContainerHeight(): number {
    const parent = this.drawerEl.parentElement;
    return parent?.getBoundingClientRect().height || window.innerHeight || 700;
  }

  private bindEvents(): void {
    this.drawerEl.addEventListener("pointerdown", (e: PointerEvent) => {
      if (!isMobileSheetViewport()) return;
      const target = e.target as HTMLElement | null;
      const handle = target?.closest<HTMLElement>("[data-role='sheet-handle']");
      if (!handle) return;
      if (e.button !== 0 && e.pointerType === "mouse") return;

      e.preventDefault();
      this.activePointerId = e.pointerId;
      this.startY = e.clientY;
      this.lastY = e.clientY;
      this.lastTs = performance.now();
      this.velocity = 0;
      this.moved = false;
      this.startHeight =
        this.drawerEl.getBoundingClientRect().height ||
        computeSheetSnapHeightPx(this.snap, this.getContainerHeight());
      this.drawerEl.classList.add("sheet-dragging");
      try {
        handle.setPointerCapture?.(e.pointerId);
      } catch {
        // Ignore pointer capture errors
      }
    });

    window.addEventListener("pointermove", (e: PointerEvent) => {
      if (this.activePointerId === null || e.pointerId !== this.activePointerId) {
        return;
      }
      const dy = e.clientY - this.startY;
      if (Math.abs(dy) > 5) this.moved = true;

      const now = performance.now();
      const dt = Math.max(1, now - this.lastTs);
      this.velocity = (e.clientY - this.lastY) / dt;
      this.lastY = e.clientY;
      this.lastTs = now;

      const containerH = this.getContainerHeight();
      const maxH = computeSheetSnapHeightPx("full", containerH);
      const nextH = Math.max(88, Math.min(maxH + 16, this.startHeight - dy));
      this.drawerEl.style.setProperty("--sheet-drag-h", `${Math.round(nextH)}px`);
    });

    const finishDrag = (e: PointerEvent): void => {
      if (this.activePointerId === null || e.pointerId !== this.activePointerId) {
        return;
      }
      this.activePointerId = null;
      this.drawerEl.classList.remove("sheet-dragging");

      if (!this.moved) {
        this.setSnap(cycleSheetSnapPoint(this.snap));
        return;
      }

      const containerH = this.getContainerHeight();
      const currentH =
        this.drawerEl.getBoundingClientRect().height || this.startHeight;
      const result = resolveSheetSnapAfterDrag({
        currentHeightPx: currentH,
        containerHeightPx: containerH,
        velocityPxPerMs: this.velocity,
        startSnap: this.snap,
      });

      this.drawerEl.style.removeProperty("--sheet-drag-h");
      if (result === "dismiss") {
        this.setSnap("half");
        this.onDismiss();
      } else {
        this.setSnap(result);
      }
    };

    window.addEventListener("pointerup", finishDrag);
    window.addEventListener("pointercancel", finishDrag);

    this.drawerEl.addEventListener("keydown", (e: KeyboardEvent) => {
      if (!isMobileSheetViewport()) return;
      const target = e.target as HTMLElement | null;
      if (!target?.closest("[data-role='sheet-handle']")) return;
      const idx = SHEET_SNAP_ORDER.indexOf(this.snap);
      if (e.key === "ArrowUp") {
        e.preventDefault();
        const next = SHEET_SNAP_ORDER[Math.min(SHEET_SNAP_ORDER.length - 1, idx + 1)]!;
        this.setSnap(next);
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        if (idx === 0) {
          this.onDismiss();
        } else {
          this.setSnap(SHEET_SNAP_ORDER[idx - 1]!);
        }
      } else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        this.setSnap(cycleSheetSnapPoint(this.snap));
      }
    });
  }
}

export function renderSheetHandleMarkup(label = "Resize or drag panel"): string {
  return `
    <div
      class="tf-sheet-handle"
      data-role="sheet-handle"
      role="button"
      tabindex="0"
      aria-label="${label} (drag up or down, tap to cycle size)"
      title="Drag to resize sheet or tap to cycle height"
    >
      <span class="tf-sheet-grip" aria-hidden="true"></span>
    </div>
  `;
}
