import { describe, expect, it } from "vitest";
import { compactValue, copyText, formatValue, safeHref, TEXT_PREVIEW_CHARS } from "./format";

describe("formatting a value conservatively", () => {
  it("marks absent and null values explicitly rather than leaving a blank", () => {
    expect(formatValue("note", null)).toMatchObject({ kind: "null", display: "null" });
    expect(formatValue("note", undefined)).toMatchObject({ kind: "null", display: "not present" });
  });

  it("shows booleans as words, not as colour", () => {
    expect(formatValue("enabled", true)).toMatchObject({ kind: "boolean", display: "true" });
    expect(formatValue("enabled", false)).toMatchObject({ kind: "boolean", display: "false" });
  });

  it("never reformats a number, and right-aligns it instead", () => {
    expect(formatValue("bytes", 1234567.891)).toMatchObject({ display: "1234567.891", alignEnd: true });
    expect(formatValue("tiny", 1e-7).display).toBe("1e-7");
    expect(formatValue("big", 9007199254740993).display).toBe("9007199254740992");
  });

  it("localizes an ISO timestamp while keeping the written value", () => {
    const formatted = formatValue("seen_at", "2026-02-02T10:30:00Z");
    expect(formatted.kind).toBe("timestamp");
    expect(formatted.full).toBe("2026-02-02T10:30:00Z");
    expect(formatted.title).toBe("2026-02-02T10:30:00Z");
    expect(formatted.display).not.toBe(formatted.full);
    // Something shaped like a date but impossible stays as written.
    expect(formatValue("seen_at", "2026-13-45").kind).toBe("text");
  });

  it("links only schemes a browser can open safely", () => {
    expect(safeHref("https://example.com/a")).toBe("https://example.com/a");
    expect(safeHref("mailto:person@example.com")).toBe("mailto:person@example.com");
    expect(safeHref("javascript:alert(1)")).toBeUndefined();
    expect(safeHref("data:text/html,<script>alert(1)</script>")).toBeUndefined();
    expect(safeHref("file:///etc/passwd")).toBeUndefined();
    expect(safeHref("not a url")).toBeUndefined();
    expect(formatValue("link", "javascript:alert(1)").href).toBeUndefined();
  });

  it("compacts a long hexadecimal value but copies all of it", () => {
    const digest = "a".repeat(64);
    const formatted = formatValue("sha256", digest);
    expect(formatted.kind).toBe("hex");
    expect(formatted.display).toContain("…");
    expect(formatted.full).toBe(digest);
    expect(formatted.monospace).toBe(true);
  });

  it("gives identifiers and host paths monospace rather than a link", () => {
    expect(formatValue("asset_id", "abc-123").monospace).toBe(true);
    expect(formatValue("path", "/var/log/syslog")).toMatchObject({ kind: "path", monospace: true });
  });

  it("badges a status-like field without deciding what the status means", () => {
    for (const value of ["passed", "failed", "weird-unknown-value"]) {
      expect(formatValue("status", value)).toMatchObject({ kind: "badge", display: value });
    }
    // A long free-text status is text, not a badge.
    expect(formatValue("status", "x".repeat(200)).kind).toBe("text");
  });

  it("truncates long text for preview while keeping the exact string", () => {
    const text = "y".repeat(TEXT_PREVIEW_CHARS + 40);
    const formatted = formatValue("description", text);
    expect(formatted.truncated).toBe(true);
    expect(formatted.full).toBe(text);
    expect(formatted.display.length).toBe(TEXT_PREVIEW_CHARS + 1);
  });

  it("preserves exact string contents, including whitespace and markup", () => {
    const markup = "<img src=x onerror=alert(1)>";
    expect(formatValue("html", markup).full).toBe(markup);
    expect(formatValue("spaced", "  keep  ").full).toBe("  keep  ");
    expect(copyText("  keep  ")).toBe("  keep  ");
  });

  it("summarises containers without claiming what is inside", () => {
    expect(compactValue([1, 2, 3])).toBe("[3]");
    expect(compactValue({ a: 1, b: 2 })).toBe("{2}");
    expect(compactValue(null)).toBe("null");
  });

  it("honours an explicit format hint over its own guess", () => {
    expect(formatValue("value", "2026-02-02T10:30:00Z", "text").kind).toBe("text");
    expect(formatValue("value", "plain", "badge").kind).toBe("badge");
    expect(formatValue("value", "plain", "code").monospace).toBe(true);
  });
});
