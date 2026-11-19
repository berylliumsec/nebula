import { describe, expect, it } from "vitest";
import { runtimeLabelRepeatsSource } from "./SessionsPage";
import { projectInitials } from "./WorkPage";

describe("runtime label", () => {
  it("treats an id that only restates the assistant name as a repeat", () => {
    expect(runtimeLabelRepeatsSource("Staging model", "staging-model")).toBe(true);
    expect(runtimeLabelRepeatsSource("GPT 5", "gpt-5")).toBe(true);
  });
  it("keeps runtime details that add information", () => {
    expect(runtimeLabelRepeatsSource("OpenRouter", "anthropic/claude-sonnet · high")).toBe(false);
    expect(runtimeLabelRepeatsSource("", "")).toBe(false);
  });
});

describe("project initials", () => {
  it("uses the first letters of the first two words", () => {
    expect(projectInitials("Staging Project 01")).toBe("SP");
    expect(projectInitials("documentation")).toBe("DO");
    expect(projectInitials("  ")).toBe("");
  });
});
