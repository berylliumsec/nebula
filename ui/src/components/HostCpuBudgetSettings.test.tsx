import {act, render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {beforeEach, describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import {HOST_CPU_BUDGET_CHANGED, type HostCpuBudget} from "../api/hostCpuBudget";
import {HostCpuBudgetSettings} from "./HostCpuBudgetSettings";

vi.mock("../diagnostics/logger", () => ({logCaughtDiagnostic: vi.fn()}));
vi.mock("./SettingsSaveFeedback", () => ({announceSettingsSaved: vi.fn()}));

const initial: HostCpuBudget = {
  host_id: "host-a", host_name: "Synthetic host A", revision: 1, cpu_limit: null,
  cpu_capacity: 4, automatic_cpu_limit: 2, effective_cpu_limit: 2,
  applied_cpu_limit: 2, apply_state: "applied", capacity_clamped: false, detail: "Applied.",
};

function client(state: HostCpuBudget = initial) {
  let value = {...state};
  const request = vi.fn(async (_path: string, options?: RequestInit) => {
    if (options?.method === "PUT") {
      const body = JSON.parse(String(options.body));
      value = {...value, cpu_limit: body.cpu_limit, revision: value.revision + 1,
        effective_cpu_limit: body.cpu_limit ?? value.automatic_cpu_limit,
        applied_cpu_limit: value.apply_state === "applied" ? body.cpu_limit ?? value.automatic_cpu_limit : null};
    }
    return {...value};
  });
  return {api: {request} as unknown as ApiClient, request};
}

describe("per-host CPU budget", () => {
  beforeEach(() => vi.clearAllMocks());

  it("saves a custom limit with host/revision and reads the observed quota back", async () => {
    const user = userEvent.setup(); const {api, request} = client();
    render(<HostCpuBudgetSettings api={api} />);
    await screen.findByText("Applied: 2 CPUs.");
    await user.selectOptions(screen.getByLabelText("CPU budget mode"), "custom");
    const input = screen.getByLabelText("Maximum background CPUs");
    await user.clear(input); await user.type(input, "3");
    await user.click(screen.getByRole("button", {name: "Save CPU budget"}));
    await screen.findByText("Saved and applied: 3 CPUs.");
    const put = request.mock.calls.find(([, options]) => options?.method === "PUT");
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({host_id: "host-a", expected_revision: 1, cpu_limit: 3});
    expect(request.mock.calls.filter(([, options]) => options?.method !== "PUT")).toHaveLength(2);
  });

  it("restores Automatic using null and the current host revision", async () => {
    const user = userEvent.setup(); const {api, request} = client({...initial, cpu_limit: 3, revision: 7, effective_cpu_limit: 3, applied_cpu_limit: 3});
    render(<HostCpuBudgetSettings api={api} />);
    await screen.findByDisplayValue("3");
    await user.selectOptions(screen.getByLabelText("CPU budget mode"), "automatic");
    await user.click(screen.getByRole("button", {name: "Save CPU budget"}));
    await screen.findByText("Saved and applied: 2 CPUs.");
    expect(JSON.parse(String(request.mock.calls.find(([, o]) => o?.method === "PUT")?.[1]?.body)).cpu_limit).toBeNull();
  });

  it("shows a saved inactive policy without claiming an applied quota", async () => {
    const user = userEvent.setup(); const {api} = client({...initial, apply_state: "inactive", applied_cpu_limit: null, detail: "Saved on this Core host. The shared background executor is inactive."});
    render(<HostCpuBudgetSettings api={api} />);
    await screen.findByText(/shared background executor is inactive/);
    await user.selectOptions(screen.getByLabelText("CPU budget mode"), "custom");
    await user.clear(screen.getByLabelText("Maximum background CPUs")); await user.type(screen.getByLabelText("Maximum background CPUs"), "3");
    await user.click(screen.getByRole("button", {name: "Save CPU budget"}));
    await waitFor(() => expect(screen.getByRole("button", {name: "Save CPU budget"})).toBeDisabled());
    expect(screen.queryByText(/Saved and applied/)).not.toBeInTheDocument();
  });

  it("rejects a limit above capacity without submitting it", async () => {
    const user = userEvent.setup(); const {api, request} = client();
    render(<HostCpuBudgetSettings api={api} />); await screen.findByText("Applied: 2 CPUs.");
    await user.selectOptions(screen.getByLabelText("CPU budget mode"), "custom");
    await user.clear(screen.getByLabelText("Maximum background CPUs")); await user.type(screen.getByLabelText("Maximum background CPUs"), "5");
    expect(screen.getByRole("alert")).toHaveTextContent("up to 4");
    expect(screen.getByRole("button", {name: "Save CPU budget"})).toBeDisabled();
    expect(request.mock.calls.some(([, options]) => options?.method === "PUT")).toBe(false);
  });

  it("retains a dirty draft when Butler changes the policy and requires reload", async () => {
    const user = userEvent.setup(); const {api} = client();
    render(<HostCpuBudgetSettings api={api} />); await screen.findByText("Applied: 2 CPUs.");
    await user.selectOptions(screen.getByLabelText("CPU budget mode"), "custom");
    await user.clear(screen.getByLabelText("Maximum background CPUs")); await user.type(screen.getByLabelText("Maximum background CPUs"), "3");
    act(() => window.dispatchEvent(new Event(HOST_CPU_BUDGET_CHANGED)));
    expect(screen.getByLabelText("Maximum background CPUs")).toHaveValue(3);
    expect(screen.getByRole("button", {name: "Save CPU budget"})).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("changed elsewhere");
    await user.click(screen.getByRole("button", {name: "Reload CPU budget"}));
    await waitFor(() => expect(screen.getByLabelText("CPU budget mode")).toHaveValue("automatic"));
  });

  it("reloads the selected host instead of keeping another host's draft", async () => {
    const user = userEvent.setup(); const a = client(); const b = client({...initial, host_id: "host-b", host_name: "Synthetic host B", cpu_capacity: 8, automatic_cpu_limit: 4, effective_cpu_limit: 4, applied_cpu_limit: 4});
    const view = render(<HostCpuBudgetSettings api={a.api} />); await screen.findByText("Applied: 2 CPUs.");
    await user.selectOptions(screen.getByLabelText("CPU budget mode"), "custom");
    await user.clear(screen.getByLabelText("Maximum background CPUs")); await user.type(screen.getByLabelText("Maximum background CPUs"), "3");
    view.rerender(<HostCpuBudgetSettings api={b.api} />);
    await screen.findByText("Applied: 4 CPUs.");
    expect(screen.getByLabelText("CPU budget mode")).toHaveValue("automatic");
    expect(screen.getByText(/Synthetic host B/)).toBeVisible();
    expect(a.request.mock.calls.some(([, o]) => o?.method === "PUT")).toBe(false);
  });

  it("keeps server conflicts visible and requires reload before another save", async () => {
    const user = userEvent.setup(); const {api, request} = client();
    const original = request.getMockImplementation()!;
    request.mockImplementation(async (path, options) => {
      if (options?.method === "PUT") throw new Error("409: This Core host or its CPU budget changed. Reload before saving.");
      return original(path, options);
    });
    render(<HostCpuBudgetSettings api={api} />); await screen.findByText("Applied: 2 CPUs.");
    await user.selectOptions(screen.getByLabelText("CPU budget mode"), "custom");
    await user.clear(screen.getByLabelText("Maximum background CPUs")); await user.type(screen.getByLabelText("Maximum background CPUs"), "3");
    await user.click(screen.getByRole("button", {name: "Save CPU budget"}));
    await screen.findByRole("alert");
    expect(screen.getByRole("button", {name: "Save CPU budget"})).toBeDisabled();
    expect(screen.getByLabelText("Maximum background CPUs")).toHaveValue(3);
  });

  it("discards an old host's late save result after the connection changes", async () => {
    const user = userEvent.setup(); const a = client(); const b = client({...initial, host_id: "host-b", host_name: "Synthetic host B", applied_cpu_limit: 4, effective_cpu_limit: 4, automatic_cpu_limit: 4, cpu_capacity: 8});
    let complete!: (value: HostCpuBudget) => void;
    a.request.mockImplementation(async (_path, options) => options?.method === "PUT"
      ? new Promise<HostCpuBudget>(resolve => {complete = resolve;}) : {...initial});
    const view = render(<HostCpuBudgetSettings api={a.api} />); await screen.findByText("Applied: 2 CPUs.");
    await user.selectOptions(screen.getByLabelText("CPU budget mode"), "custom");
    await user.clear(screen.getByLabelText("Maximum background CPUs")); await user.type(screen.getByLabelText("Maximum background CPUs"), "3");
    await user.click(screen.getByRole("button", {name: "Save CPU budget"}));
    await waitFor(() => expect(complete).toBeDefined());
    view.rerender(<HostCpuBudgetSettings api={b.api} />); await screen.findByText("Applied: 4 CPUs.");
    await act(async () => {complete({...initial, cpu_limit: 3, revision: 2, applied_cpu_limit: 3, effective_cpu_limit: 3});});
    expect(screen.getByText("Applied: 4 CPUs.")).toBeVisible();
    expect(screen.queryByText("Saved and applied: 3 CPUs.")).not.toBeInTheDocument();
  });
});
