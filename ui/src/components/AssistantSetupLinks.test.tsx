import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Boxes, Settings2 } from "lucide-react";
import { expect, it, vi } from "vitest";
import { settingCatalogEntry } from "../settingsCatalog";
import { AssistantSetupLinks } from "./AssistantSetupLinks";

it("presents assistant infrastructure as compact focused-setting links", async () => {
  const onOpen = vi.fn();
  const providers = settingCatalogEntry("settings.providers");
  const mcp = settingCatalogEntry("settings.mcp");
  render(<AssistantSetupLinks items={[
    { entry: providers, icon: Settings2, label: "Model providers", detail: "2 enabled" },
    { entry: mcp, icon: Boxes, label: "MCP tools", detail: "None configured" },
  ]} onOpen={onOpen} />);

  const setup = screen.getByRole("region", { name: "Assistant setup" });
  expect(within(setup).getAllByRole("button")).toHaveLength(2);
  expect(within(setup).getByRole("button", { name: "Model providers, 2 enabled" })).toBeVisible();
  await userEvent.click(within(setup).getByRole("button", { name: "MCP tools, None configured" }));
  expect(onOpen).toHaveBeenCalledWith(mcp);
});
