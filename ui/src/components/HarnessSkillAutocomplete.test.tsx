import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { HarnessSkillSummary } from "../api/types";
import { HarnessSkillAutocomplete, findHarnessSkillToken } from "./HarnessSkillAutocomplete";

const skills: HarnessSkillSummary[] = [
  { name: "review", path: "/workspace/.agents/skills/review/SKILL.md", source: "project" },
  { name: "report", path: "/workspace/.agents/skills/report/SKILL.md", source: "installed" },
];

describe("HarnessSkillAutocomplete", () => {
  it("finds only a dollar token at a token boundary", () => {
    expect(findHarnessSkillToken("run $rev", 8)).toEqual({ start: 4, end: 8, query: "rev" });
    expect(findHarnessSkillToken("cash$rev", 8)).toBeUndefined();
  });

  it("renders filtered options and keeps click selection operator-friendly", () => {
    const onSelect = vi.fn();
    render(
      <HarnessSkillAutocomplete
        skills={skills}
        token={{ start: 4, end: 8, query: "rev" }}
        activeIndex={0}
        onActiveIndexChange={() => undefined}
        onSelect={onSelect}
        onClose={() => undefined}
      />,
    );
    expect(screen.getByRole("option", { name: /\$review/ })).toBeVisible();
    expect(screen.queryByRole("option", { name: /\$report/ })).toBeNull();
    fireEvent.mouseDown(screen.getByRole("option", { name: /\$review/ }));
    expect(onSelect).toHaveBeenCalledWith(skills[0]);
  });
});
