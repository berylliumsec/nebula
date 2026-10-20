import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, expect, it } from "vitest";
import type { ApiClient } from "../api/client";
import type { RuntimeUsageResponse } from "../api/types";
import { RuntimeUsagePanel } from "./RuntimeUsagePanel";

const snapshot: RuntimeUsageResponse = {
  generatedAt: "2026-10-12T12:00:00Z",
  items: [
    {
      kind: "provider", profileId: "router", name: "OpenRouter Research", providerType: "openrouter", enabled: true,
      recorded: { inputTokens: 1200, outputTokens: 300, totalTokens: 1500, cachedInputTokens: 400, turns: 3, scope: "Nebula chat turns" },
      vendor: { state: "available", source: "OpenRouter key", detail: "This is the API key allowance, not the account's total credits.", checkedAt: "2026-10-12T12:00:00Z", balances: [{ label: "Key allowance", currency: "USD", remaining: 74.5, used: 25.5, limit: 100, note: "Key cap only" }], windows: [] },
    },
    {
      kind: "harness", profileId: "grok", name: "Grok Build", providerType: "grok_acp", enabled: false,
      recorded: { inputTokens: 20, outputTokens: 5, totalTokens: 25, cachedInputTokens: 0, turns: 1, scope: "Nebula harness turns" },
      vendor: { state: "available", source: "Grok account", detail: "Grok reported included-credit use for this period. A shared pool may include other Grok products.", balances: [], windows: [{ label: "Shared Grok credits · Weekly", usedPercent: 68 }] },
    },
  ],
};

it("shows measured activity and honest vendor scope, then recovers a failed refresh", async () => {
  const getRuntimeUsage = vi.fn().mockResolvedValueOnce(snapshot).mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({
    ...snapshot, items: [{ ...snapshot.items[0], vendor: { ...snapshot.items[0].vendor, balances: [{ ...snapshot.items[0].vendor.balances[0], remaining: 60 }] } }, snapshot.items[1]],
  });
  const api = { getRuntimeUsage } as unknown as ApiClient;
  const user = userEvent.setup();
  render(<RuntimeUsagePanel api={api} active />);
  const router = await screen.findByRole("article", { name: "OpenRouter Research usage" });
  expect(router).toHaveTextContent("$74.50");
  expect(router).toHaveTextContent("not the account's total credits");
  expect(router).toHaveTextContent("1,500");
  const grok = screen.getByRole("article", { name: "Grok Build usage" });
  expect(grok).toHaveTextContent("Shared Grok credits");
  expect(grok).toHaveTextContent("32% left");
  expect(grok).toHaveTextContent("25");
  expect(grok).not.toHaveTextContent("$0.00");
  await user.click(within(router).getByText("Usage details"));
  expect(router).toHaveTextContent("400 tokens");

  await user.click(screen.getByRole("button", { name: "Refresh provider and harness usage" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("earlier figures remain visible");
  expect(router).toHaveTextContent("$74.50");
  await user.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(router).toHaveTextContent("$60.00"));
  expect(getRuntimeUsage).toHaveBeenNthCalledWith(2, true, expect.any(AbortSignal));
});

it("shows Codex and Grok credits when included windows have no remaining usage", async () => {
  const base = snapshot.items[1];
  const items: RuntimeUsageResponse["items"] = [
    { ...base, profileId: "codex", name: "Codex", providerType: "codex_app_server", enabled: true,
      vendor: { state: "available", source: "Codex account", detail: "Credits are separate from included usage.",
        balances: [{ label: "Codex credits", remaining: 7.25, note: "Vendor credit units" }],
        windows: [{ label: "Included usage", usedPercent: 100 }] } },
    { ...base, vendor: { state: "available", source: "Grok account", detail: "Prepaid credits are separate from included usage.",
      balances: [{ label: "Grok prepaid credits", remaining: 3.75, note: "Vendor credit units" }],
      windows: [{ label: "Shared Grok credits · Weekly", usedPercent: 100 }] } },
  ];
  const api = { getRuntimeUsage: vi.fn().mockResolvedValue({ ...snapshot, items }) } as unknown as ApiClient;
  render(<RuntimeUsagePanel api={api} active />);
  expect(await screen.findByRole("article", { name: "Codex usage" })).toHaveTextContent("7.25");
  expect(screen.getByRole("article", { name: "Grok Build usage" })).toHaveTextContent("3.75");
  expect(screen.getAllByText("0% left")).toHaveLength(2);
});
