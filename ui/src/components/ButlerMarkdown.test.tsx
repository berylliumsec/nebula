import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ButlerMarkdown } from "./ButlerMarkdown";

const answer = `**Candidates (2)**

| Item | Status | Last update |
| --- | --- | ---: |
| Sample parser fixture: synthetic description (\`a1b2c3d4\`) | review | Nov 26 |
| Sample upload form: retry handling (\`e5f6a7b8\`) | in progress | Nov 24 |`;

describe("ButlerMarkdown", () => {
  it("puts a Butler table in its own scrollable, focusable region with a real header row", () => {
    render(<ButlerMarkdown content={answer} />);
    const region = screen.getByRole("region", { name: "Butler response table" });
    expect(region).toHaveAttribute("tabindex", "0");
    const table = within(region).getByRole("table");
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Item", "Status", "Last update"]);
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByText("a1b2c3d4").tagName).toBe("CODE");
  });

  it("keeps a column alignment the Markdown asked for", () => {
    render(<ButlerMarkdown content={answer} />);
    const [, , lastUpdate] = screen.getAllByRole("columnheader");
    expect(lastUpdate.getAttribute("style") ?? lastUpdate.getAttribute("align")).toMatch(/right/);
  });

  it("renders plain prose without a table region", () => {
    render(<ButlerMarkdown content="All three checks **passed**." />);
    expect(screen.queryByRole("region")).toBeNull();
    expect(screen.getByText("passed").tagName).toBe("STRONG");
  });
});
