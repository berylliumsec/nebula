import { describe, expect, it } from "vitest";
import { latestProgressPreview, splitSavedProgress } from "./chatProgressContent";

describe("saved provider progress", () => {
  it("separates routing updates from the answer at Core's UTF-16 boundary", () => {
    const progress = "Checking 🔎 the index.\n\nDispatching the missing-evidence work.";
    const content = `${progress}\n\nThe final answer is ready.`;
    expect(splitSavedProgress(content, { progress_prefix_utf16_length: progress.length })).toEqual({
      progress,
      answer: "The final answer is ready.",
    });
  });

  it("keeps legacy and malformed saved content intact", () => {
    const content = "A complete answer with\n\nmore than one paragraph.";
    expect(splitSavedProgress(content, {})).toEqual({ answer: content });
    expect(splitSavedProgress(content, { progress_prefix_utf16_length: 4 })).toEqual({ answer: content });
    expect(splitSavedProgress(content, { progress_prefix_utf16_length: Number.NaN })).toEqual({ answer: content });
  });

  it("keeps an answer intact if a code fence spans the saved boundary", () => {
    const progress = "Opening a code example.\n\n```bash\necho ready";
    const content = `${progress}\n\n\`\`\`\nFinal answer.`;
    expect(splitSavedProgress(content, { progress_prefix_utf16_length: progress.length })).toEqual({ answer: content });
  });

  it("previews only the latest paragraph and keeps long Unicode text bounded", () => {
    expect(latestProgressPreview("First update.\n\nLatest 🔎 update.\n"))
      .toBe("Latest 🔎 update.");
    const preview = latestProgressPreview(`First.\n\n${"🔎".repeat(260)}`);
    expect(Array.from(preview)).toHaveLength(240);
    expect(preview.endsWith("…")).toBe(true);
  });
});
