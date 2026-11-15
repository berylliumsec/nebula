import { describe, expect, it } from "vitest";
import { placeSelectionToolbar, type PlacementRect } from "./toolbarPlacement";

const rect = (left: number, top: number, width: number, height: number): PlacementRect => ({ left, top, right: left + width, bottom: top + height });
const overlaps = (a: PlacementRect, b: PlacementRect) => Math.min(a.right, b.right) > Math.max(a.left, b.left) && Math.min(a.bottom, b.bottom) > Math.max(a.top, b.top);

// Geometry measured from the Add asset dialog in Chromium (#1096).
const phone = {
  viewport: { width: 390, height: 844 },
  size: { width: 366, height: 101 },
  anchor: rect(29, 703, 332, 40), // Tags input
  dialog: rect(8, 145, 374, 699),
  actions: [rect(29, 765, 160, 44), rect(201, 765, 160, 44), rect(326, 172, 28, 28)],
  fields: [rect(29, 272, 332, 38), rect(29, 630, 332, 38)],
};

describe("selection toolbar placement", () => {
  it("puts the toolbar under a page selection when it fits, as before", () => {
    const placement = placeSelectionToolbar({ viewport: { width: 1440, height: 900 }, size: { width: 372, height: 54 }, anchor: rect(600, 300, 80, 20) });
    expect(placement).toEqual({ left: 454, top: 328, side: "below" });
  });

  it("uses its measured height, so a wrapped phone toolbar never runs off screen", () => {
    const placement = placeSelectionToolbar({ viewport: phone.viewport, size: phone.size, anchor: rect(29, 760, 100, 20) });
    expect(placement?.side).toBe("above");
    expect(placement!.top + phone.size.height).toBeLessThanOrEqual(844 - 12);
  });

  it("never covers a dialog's buttons at phone width", () => {
    const placement = placeSelectionToolbar(phone)!;
    const box = rect(placement.left, placement.top, phone.size.width, phone.size.height);
    for (const action of phone.actions) expect(overlaps(box, action)).toBe(false);
    for (const field of phone.fields) expect(overlaps(box, field)).toBe(false);
    expect(box.bottom).toBeLessThanOrEqual(phone.dialog.top);
  });

  it("never covers a dialog's footer on desktop", () => {
    const footer = [rect(780, 652, 90, 40), rect(880, 652, 110, 40)];
    const input = { viewport: { width: 1440, height: 900 }, size: { width: 372, height: 54 }, anchor: rect(450, 596, 540, 40), dialog: rect(430, 120, 580, 600), actions: footer, fields: [rect(450, 520, 260, 40)] };
    const placement = placeSelectionToolbar(input)!;
    const box = rect(placement.left, placement.top, 372, 54);
    for (const action of footer) expect(overlaps(box, action)).toBe(false);
    expect(overlaps(box, input.anchor)).toBe(false);
  });

  it("covers another field rather than a button when nothing else is clear", () => {
    const input = { viewport: { width: 320, height: 568 }, size: { width: 296, height: 101 }, anchor: rect(20, 420, 280, 40), dialog: rect(0, 0, 320, 568), actions: [rect(20, 480, 280, 44), rect(280, 12, 28, 28)], fields: [rect(20, 300, 280, 100)] };
    const placement = placeSelectionToolbar(input)!;
    const box = rect(placement.left, placement.top, 296, 101);
    for (const action of input.actions) expect(overlaps(box, action)).toBe(false);
    expect(overlaps(box, input.anchor)).toBe(false);
  });

  it("stays hidden in a dialog with no spot clear of its buttons", () => {
    const placement = placeSelectionToolbar({ viewport: { width: 320, height: 568 }, size: { width: 296, height: 101 }, anchor: rect(20, 200, 280, 40), dialog: rect(0, 0, 320, 568), actions: [rect(0, 0, 320, 190), rect(0, 250, 320, 318)] });
    expect(placement).toBeNull();
  });
});
