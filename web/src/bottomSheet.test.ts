import { describe, expect, it } from "vitest";
import {
  computeSheetSnapHeightPx,
  cycleSheetSnapPoint,
  isMobileSheetViewport,
  resolveSheetSnapAfterDrag,
} from "./bottomSheet";

describe("bottomSheet — mobile draggable sheet detents and gestures", () => {
  it("computes monotonic peek < half < full snap heights within container bounds", () => {
    const peek = computeSheetSnapHeightPx("peek", 720);
    const half = computeSheetSnapHeightPx("half", 720);
    const full = computeSheetSnapHeightPx("full", 720);

    expect(peek).toBeGreaterThanOrEqual(220);
    expect(half).toBeGreaterThan(peek);
    expect(full).toBeGreaterThan(half);
    expect(full).toBeLessThan(720);
  });

  it("snaps cleanly based on release position and flick velocity", () => {
    // Dragged near full -> snaps to full
    expect(
      resolveSheetSnapAfterDrag({
        currentHeightPx: 620,
        containerHeightPx: 720,
        velocityPxPerMs: 0,
        startSnap: "half",
      })
    ).toBe("full");

    // Dragged far below peek -> dismisses sheet
    expect(
      resolveSheetSnapAfterDrag({
        currentHeightPx: 110,
        containerHeightPx: 720,
        velocityPxPerMs: 0.1,
        startSnap: "peek",
      })
    ).toBe("dismiss");

    // Fast upward flick from peek -> half
    expect(
      resolveSheetSnapAfterDrag({
        currentHeightPx: 260,
        containerHeightPx: 720,
        velocityPxPerMs: -0.9,
        startSnap: "peek",
      })
    ).toBe("half");

    // Fast downward flick from peek -> dismiss
    expect(
      resolveSheetSnapAfterDrag({
        currentHeightPx: 220,
        containerHeightPx: 720,
        velocityPxPerMs: 0.9,
        startSnap: "peek",
      })
    ).toBe("dismiss");
  });

  it("cycles detents and identifies mobile viewports accurately", () => {
    expect(cycleSheetSnapPoint("peek")).toBe("half");
    expect(cycleSheetSnapPoint("half")).toBe("full");
    expect(cycleSheetSnapPoint("full")).toBe("peek");
    expect(isMobileSheetViewport(390)).toBe(true);
    expect(isMobileSheetViewport(768)).toBe(true);
    expect(isMobileSheetViewport(1024)).toBe(false);
  });
});
