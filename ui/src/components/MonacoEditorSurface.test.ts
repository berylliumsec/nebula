import { describe, expect, it } from "vitest";
import { languageIdForPath, languageLabelForPath } from "./editorLanguages";

describe("Monaco language selection", () => {
  it("labels supported workspace languages and falls back to plain text", () => {
    expect(languageLabelForPath("scripts/check.py")).toBe("Python");
    expect(languageLabelForPath("scripts/scan.sh")).toBe("Shell");
    expect(languageLabelForPath("frontend/view.tsx")).toBe("TypeScript");
    expect(languageLabelForPath("config/rules.yaml")).toBe("YAML");
    expect(languageLabelForPath("README.unknown")).toBe("Plain text");
    expect(languageIdForPath("scripts/check.py")).toBe("python");
    expect(languageIdForPath("frontend/view.tsx")).toBe("typescript");
    expect(languageIdForPath("README.unknown")).toBe("plaintext");
  });
});
