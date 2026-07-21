import { describe, expect, it } from "vitest";
import { calculateImageEditorFitZoom } from "./ImageEditor";

describe("calculateImageEditorFitZoom", () => {
  it("fits a tall terminal capture below ten percent", () => {
    const zoom = calculateImageEditorFitZoom(2_387, 16_384, 1_846, 940);

    expect(zoom).toBeCloseTo(940 / 16_384);
    expect(zoom).toBeLessThan(0.1);
    expect(16_384 * zoom).toBeLessThanOrEqual(940);
  });

  it("does not enlarge an image that already fits", () => {
    expect(calculateImageEditorFitZoom(800, 600, 1_600, 900)).toBe(1);
  });

  it("honors the available space even below the manual zoom floor", () => {
    expect(calculateImageEditorFitZoom(16_384, 16_384, 1, 1)).toBe(1 / 16_384);
  });
});
