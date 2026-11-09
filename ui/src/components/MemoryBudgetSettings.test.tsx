import {act, render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import {GIB, MEMORY_BUDGET_CHANGED, type MemorySettings} from "../api/memoryBudgets";
import {MemoryBudgetSettings} from "./MemoryBudgetSettings";

const initial: MemorySettings = {
  revision: 1, policy: {default_job: {max_bytes: 2 * GIB, high_bytes: null, swap_bytes: 0}, aggregate_bytes: 12 * GIB, reserve_bytes: 2 * GIB, environment_defaults: {}, profile_overrides: {}},
  administrator_limits: {capacity_bytes: 16 * GIB, maximum_job_bytes: 8 * GIB, maximum_aggregate_bytes: 14 * GIB, minimum_reserve_bytes: 2 * GIB, maximum_swap_bytes: 2 * GIB},
  capabilities: [], profiles: [], reserved_bytes: 0, available_bytes: 12 * GIB, effective_aggregate_bytes: 12 * GIB, held_jobs: 0, queued_jobs: 0,
  application_state: "unavailable", detail: "Policy saved. No runner has verified enforcement.",
};
function fixture(atLimit = false, observed: Partial<MemorySettings> = {}) {
  let saved = {...structuredClone(initial), ...observed};
  const request = vi.fn(async (path: string, options?: RequestInit) => {
    if (path === "memory-budgets/jobs") return {items: atLimit ? [{id: "queued", resource_kind: "fixture", resource_id: "older-job", state: "queued", application_state: "not_started", snapshot: {limits: initial.policy.default_job, policy_revision: 1}, evidence: null}] : [], at_limit: atLimit, display_limit: 1};
    if (options?.method === "PUT") {const body = JSON.parse(String(options.body)); if (body.expected_revision !== saved.revision) throw new Error("409: budgets changed; reload"); saved = {...saved, revision: saved.revision + 1, policy: body.policy};}
    return structuredClone(saved);
  });
  return {api: {request} as unknown as ApiClient, request, changeElsewhere: () => {saved.revision++;}};
}

describe("Memory budgets", () => {
  it("uses the observed resident-runtime minimum and saves a valid Core reserve", async () => {
    const user = userEvent.setup();
    const {api, request} = fixture(false, {runtime_reserved_bytes: GIB / 2, minimum_core_reserve_bytes: 2.5 * GIB, policy: {...initial.policy, reserve_bytes: 3 * GIB}});
    render(<MemoryBudgetSettings api={api} />);
    const reserve = await screen.findByLabelText(/Core and services reserve/);
    expect(reserve).toHaveAttribute("min", "2.5");
    expect(screen.getByText(/Resident runtimes reserve 0.5 GiB/)).toBeVisible();
    await user.clear(reserve); await user.type(reserve, "2.25");
    await user.click(screen.getByRole("button", {name: "Save memory budgets"}));
    expect(request.mock.calls.some(([, options]) => options?.method === "PUT")).toBe(false);
    await user.clear(reserve); await user.type(reserve, "2.5");
    await user.click(screen.getByRole("button", {name: "Save memory budgets"}));
    await screen.findByText(/Saved and read back/);
    expect(JSON.parse(String(request.mock.calls.find(([, options]) => options?.method === "PUT")?.[1]?.body)).policy.reserve_bytes).toBe(2.5 * GIB);
  });
  it("identifies a bounded job page without claiming a complete roster", async () => {
    const {api} = fixture(true);
    render(<MemoryBudgetSettings api={api} />);
    expect(await screen.findByText("Showing up to 1 job, charged jobs first. More jobs may exist.")).toBeVisible();
    expect(screen.getByText("fixture · older-job")).toBeVisible();
  });
  it("saves through Core and reads back without claiming worker application", async () => {
    const user = userEvent.setup(); const {api, request} = fixture();
    render(<MemoryBudgetSettings api={api} />);
    const ceiling = await screen.findByLabelText(/Per job ceiling/);
    await user.clear(ceiling); await user.type(ceiling, "4");
    await user.click(screen.getByRole("button", {name: "Save memory budgets"}));
    await screen.findByText("Saved and read back. New jobs use this policy; existing job limits are unchanged.");
    const call = request.mock.calls.find(([, options]) => options?.method === "PUT")!;
    expect(JSON.parse(String(call[1]?.body))).toMatchObject({expected_revision: 1, policy: {default_job: {max_bytes: 4 * GIB}}});
    expect(screen.getByText("Policy saved. No runner has verified enforcement.")).toBeVisible();
    expect(screen.queryByText(/Saved and applied/)).not.toBeInTheDocument();
    expect(request.mock.calls.filter(([path, options]) => path === "memory-budgets" && !options?.method).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole("button", {name: "Save memory budgets"})).toBeDisabled();
  });

  it("keeps a conflicting draft and reloads authoritative values", async () => {
    const user = userEvent.setup(); const {api, changeElsewhere} = fixture();
    render(<MemoryBudgetSettings api={api} />);
    const ceiling = await screen.findByLabelText(/Per job ceiling/);
    await user.clear(ceiling); await user.type(ceiling, "4");
    changeElsewhere(); act(() => window.dispatchEvent(new Event(MEMORY_BUDGET_CHANGED)));
    await screen.findByText("Memory budgets changed elsewhere. Reload before saving your draft.");
    expect(ceiling).toHaveValue(4);
    expect(screen.getByRole("button", {name: "Save memory budgets"})).toBeDisabled();
    await user.click(screen.getByRole("button", {name: "Reload memory budgets"}));
    await waitFor(() => expect(screen.getByLabelText(/Per job ceiling/)).toHaveValue(2));
  });

  it("offers bounded environment overrides and explains unavailable pressure support", async () => {
    const user = userEvent.setup(); const {api, request} = fixture();
    render(<MemoryBudgetSettings api={api} />);
    await screen.findByLabelText(/Per job ceiling/);
    expect(screen.getByLabelText(/Per job pressure/)).toBeDisabled();
    await user.click(screen.getByText("Environment and execution-profile defaults"));
    await user.click(screen.getByLabelText("Override container defaults"));
    const field = screen.getByLabelText(/container ceiling/);
    await user.clear(field); await user.type(field, "1");
    await user.click(screen.getByRole("button", {name: "Save memory budgets"}));
    await screen.findByText(/Saved and read back/);
    expect(JSON.parse(String(request.mock.calls.find(([, options]) => options?.method === "PUT")?.[1]?.body)).policy.environment_defaults.container.max_bytes).toBe(GIB);
  });

  it("shows an actionable load failure and retries from the visible entry point", async () => {
    const user = userEvent.setup(); const {api, request} = fixture(); request.mockRejectedValueOnce(new Error("Synthetic connection lost"));
    render(<MemoryBudgetSettings api={api} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Synthetic connection lost");
    await user.click(screen.getByRole("button", {name: "Reload memory budgets"}));
    await screen.findByLabelText(/Per job ceiling/);
  });
});
