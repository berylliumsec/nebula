import { describe, expect, it } from "vitest";
import { fileGlyph } from "./fileIcons";

describe("file glyphs", () => {
  it("gives each language its own colour so a long listing is scannable", () => {
    const typescript = fileGlyph("src/components/CodeEditorPanel.tsx");
    const python = fileGlyph("scans/scan.py");
    const css = fileGlyph("ui/src/tokens.css");
    expect(new Set([typescript.color, python.color, css.color]).size).toBe(3);
  });

  it("reads the extension from the file name, not from the path", () => {
    expect(fileGlyph("reports.css/notes.md").color).toBe(fileGlyph("notes.md").color);
  });

  it("falls back for unknown and extensionless files, and marks directories", () => {
    expect(fileGlyph("Makefile")).toEqual(fileGlyph("payload.unknown"));
    expect(fileGlyph("src", "directory").color).toBe("var(--blue)");
  });
});
