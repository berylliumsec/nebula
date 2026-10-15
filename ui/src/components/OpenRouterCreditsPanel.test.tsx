import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { OpenRouterCreditsPanel } from "./OpenRouterCreditsPanel";

function client(request: ReturnType<typeof vi.fn>): ApiClient {
  return { request } as unknown as ApiClient;
}

describe("OpenRouterCreditsPanel", () => {
  it("shows a measured account balance and threshold separately from the inference key cap", async () => {
    const request = vi.fn().mockResolvedValue({
      provider_id: "router-a", status: "observed", checked_at: "2026-10-15T14:00:00Z",
      observed_at: "2026-10-15T14:00:00Z", total_credits_usd: 50,
      total_usage_usd: 32, remaining_usd: 18,
    });
    render(<OpenRouterCreditsPanel api={client(request)} providerId="router-a" providerName="Team Router" onConfigure={vi.fn()} />);

    expect(await screen.findByText("$18.00 remaining")).toBeVisible();
    expect(screen.getByText("At or below the $20 account balance threshold.")).toBeVisible();
    expect(screen.getByText(/separate from the inference key spending limit/)).toBeVisible();
    expect(request).toHaveBeenCalledWith("providers/router-a/openrouter/credits/refresh", {
      method: "POST", signal: expect.any(AbortSignal),
    });
  });

  it("explains a missing management key and opens provider setup", async () => {
    const user = userEvent.setup();
    const onConfigure = vi.fn();
    const request = vi.fn().mockResolvedValue({
      provider_id: "router-a", status: "unavailable", checked_at: "2026-10-15T14:00:00Z",
      remaining_usd: null, reason_code: "management_credential_missing",
    });
    render(<OpenRouterCreditsPanel api={client(request)} providerId="router-a" providerName="Team Router" onConfigure={onConfigure} />);

    expect(await screen.findByText("Add an OpenRouter management key to check account credits.")).toBeVisible();
    expect(screen.queryByText(/remaining$/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Configure management key" }));
    expect(onConfigure).toHaveBeenCalledOnce();
  });

  it("offers Retry after an error and never shows the previous provider's balance", async () => {
    const user = userEvent.setup();
    const request = vi.fn()
      .mockResolvedValueOnce({ provider_id: "router-a", status: "observed", checked_at: "2026-10-15T14:00:00Z", remaining_usd: 18 })
      .mockRejectedValueOnce(new Error("OpenRouter is unavailable"))
      .mockResolvedValueOnce({ provider_id: "router-b", status: "observed", checked_at: "2026-10-15T15:00:00Z", remaining_usd: 90 });
    const api = client(request);
    const view = render(<OpenRouterCreditsPanel api={api} providerId="router-a" providerName="First" onConfigure={vi.fn()} />);
    expect(await screen.findByText("$18.00 remaining")).toBeVisible();

    view.rerender(<OpenRouterCreditsPanel api={api} providerId="router-b" providerName="Second" onConfigure={vi.fn()} />);
    expect(screen.queryByText("$18.00 remaining")).not.toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("OpenRouter is unavailable");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText("$90.00 remaining")).toBeVisible());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
