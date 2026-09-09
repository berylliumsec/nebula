import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ApplicationModelCategory } from "./ApplicationModelOutline";
import { blankClaim, type GraphObject } from "../pages/applicationModelTypes";
const objects: GraphObject[] = Array.from({ length: 2000 }, (_, i) => ({
  id: String(i),
  label: `API ${i}`,
  classification: blankClaim("Endpoint"),
  authentication_context: "anonymous",
  properties: {},
  revision: 1,
}));
describe("large model categories", () => {
  it("collapses thousands of objects and renders only one page when opened", () => {
    const { container } = render(
      <ApplicationModelCategory
        name="APIs"
        objects={objects}
        selected=""
        query=""
        onSelect={vi.fn()}
      />,
    );
    expect(container.querySelectorAll(".am-object")).toHaveLength(0);
    const details = container.querySelector("details")!;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    expect(container.querySelectorAll(".am-object")).toHaveLength(20);
    fireEvent.click(screen.getByRole("button", { name: "Next APIs objects" }));
    expect(
      screen.getByRole("button", { name: /API 20.*Endpoint/ }),
    ).toBeVisible();
    expect(container.querySelectorAll(".am-object")).toHaveLength(20);
  });
  it("opens the page containing a deep-linked object near the end", () => {
    const select = vi.fn();
    const { container } = render(
      <ApplicationModelCategory
        name="APIs"
        objects={objects}
        selected="1999"
        query=""
        onSelect={select}
      />,
    );
    expect(container.querySelectorAll(".am-object")).toHaveLength(20);
    fireEvent.click(screen.getByRole("button", { name: /API 1999.*Endpoint/ }));
    expect(select).toHaveBeenCalledWith("1999");
  });
});
