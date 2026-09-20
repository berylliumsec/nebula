import { describe, expect, it } from "vitest";

import { elapsedDetail, elapsedSince, formatLiveElapsed, formatTurnElapsed } from "./turnElapsed";

describe("formatTurnElapsed", () => {
  it("keeps one decimal under a minute", () => {
    expect(formatTurnElapsed(8_240)).toBe("8.2s");
    expect(formatTurnElapsed(420)).toBe("0.4s");
  });

  it("pads the seconds once a turn passes a minute", () => {
    expect(formatTurnElapsed(252_000)).toBe("4m 12s");
    expect(formatTurnElapsed(65_000)).toBe("1m 05s");
  });

  it("drops to hours and minutes for very long turns", () => {
    expect(formatTurnElapsed(3_840_000)).toBe("1h 04m");
  });

  it("treats missing or negative values as zero", () => {
    expect(formatTurnElapsed(-5)).toBe("0.0s");
    expect(formatTurnElapsed(Number.NaN)).toBe("0.0s");
  });
});

describe("formatLiveElapsed", () => {
  it("counts up as a stopwatch", () => {
    expect(formatLiveElapsed(12_400)).toBe("0:12");
    expect(formatLiveElapsed(725_000)).toBe("12:05");
    expect(formatLiveElapsed(3_751_000)).toBe("1:02:31");
  });
});

describe("elapsedDetail", () => {
  it("is absent when Core reported no elapsed time", () => {
    expect(elapsedDetail(undefined, 1_400)).toBeUndefined();
  });

  it("names the approval wait only when there was one", () => {
    expect(elapsedDetail(8_240)).toBe("8.2s elapsed");
    expect(elapsedDetail(8_240, 0)).toBe("8.2s elapsed");
    expect(elapsedDetail(8_240, 1_400)).toBe("8.2s elapsed · 1.4s waiting for approval");
  });
});

describe("elapsedSince", () => {
  it("measures forward from the turn's start", () => {
    expect(elapsedSince("2026-09-19T10:00:00.000Z", Date.parse("2026-09-19T10:00:08.200Z"))).toBe(8_200);
  });

  it("never runs backwards and ignores unreadable timestamps", () => {
    expect(elapsedSince("2026-09-19T10:00:10.000Z", Date.parse("2026-09-19T10:00:00.000Z"))).toBe(0);
    expect(elapsedSince("not a timestamp")).toBeUndefined();
  });
});
