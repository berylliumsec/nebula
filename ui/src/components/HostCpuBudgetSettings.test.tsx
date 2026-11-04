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

const disabled: HostCpuBudget = {...initial, enabled: false, activation_supported: true,
  activation_unavailable_reason: null, executor_scope: "report-pdfs", executor_state: "disabled",
  worker_count: 0, active_jobs: 0, queued_jobs: 0, maximum_worker_cpus: 4,
  applied_cpu_limit: null, apply_state: "inactive", detail: "Background PDF workers are disabled."};

function managedClient(start: HostCpuBudget = disabled) {
  let value = {...start};
  const request = vi.fn(async (_path: string, options?: RequestInit) => {
    if (options?.method === "PUT") {
      const body = JSON.parse(String(options.body));
      value = {...value, enabled: body.enabled, cpu_limit: body.cpu_limit, revision: value.revision + 1,
        apply_state: "pending", executor_state: value.worker_count ? "draining" : "starting",
        detail: value.worker_count ? "Draining accepted PDF work before applying the worker setting." : "Starting background PDF workers…"};
    }
    return {...value};
  });
  return {api: {request} as unknown as ApiClient, request, complete: (facts: Partial<HostCpuBudget>) => {value = {...value, ...facts};}};
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

  it("saves explicit activation and waits for observed running workers", async () => {
    const user = userEvent.setup(); const {api, request, complete} = managedClient();
    render(<HostCpuBudgetSettings api={api} />);
    const toggle = await screen.findByLabelText("Enable background PDF workers");
    expect(toggle).not.toBeChecked();
    await user.click(toggle);
    expect(request.mock.calls.some(([, o]) => o?.method === "PUT")).toBe(false);
    await user.click(screen.getByRole("button", {name: "Save CPU budget"}));
    await screen.findByText("Starting background PDF workers…");
    expect(JSON.parse(String(request.mock.calls.find(([, o]) => o?.method === "PUT")?.[1]?.body)))
      .toEqual({host_id: "host-a", expected_revision: 1, cpu_limit: null, enabled: true});
    expect(screen.queryByText(/Running 2 PDF/)).not.toBeInTheDocument();
    complete({apply_state: "applied", executor_state: "running", worker_count: 2, applied_cpu_limit: 2, detail: "Running 2 PDF workers on 2 CPUs."});
    await screen.findByText("Running 2 PDF workers on 2 CPUs.", {}, {timeout: 2000});
    expect(toggle).toBeChecked();
    expect(screen.queryByText("Starting background PDF workers…")).not.toBeInTheDocument();
  });

  it("shows draining after disable until Core reports zero workers", async () => {
    const user = userEvent.setup(); const {api, request, complete} = managedClient({...disabled, enabled: true, worker_count: 2,
      executor_state: "running", apply_state: "applied", applied_cpu_limit: 2, detail: "Running 2 PDF workers on 2 CPUs."});
    render(<HostCpuBudgetSettings api={api} />);
    const toggle = await screen.findByLabelText("Enable background PDF workers");
    await user.click(toggle); await user.click(screen.getByRole("button", {name: "Save CPU budget"}));
    await screen.findByText(/Draining accepted PDF/);
    expect(JSON.parse(String(request.mock.calls.find(([, o]) => o?.method === "PUT")?.[1]?.body)).enabled).toBe(false);
    expect(screen.getByText("2 workers · 0 active · 0 queued")).toBeVisible();
    complete({apply_state: "inactive", executor_state: "disabled", worker_count: 0, applied_cpu_limit: null, detail: disabled.detail});
    await screen.findByText(disabled.detail, {}, {timeout: 2000});
    expect(screen.getByText("0 workers · 0 active · 0 queued")).toBeVisible();
  });

  it("keeps unavailable worker counts unknown and recovers from a fresh Core read", async () => {
    const {api, complete} = managedClient({...disabled, enabled: true, executor_state: "unavailable",
      apply_state: "unavailable", worker_count: null, active_jobs: null, queued_jobs: null,
      detail: "Worker status could not be checked. Reload to retry."});
    render(<HostCpuBudgetSettings api={api} />);
    await screen.findByText("Worker counts unavailable.");
    expect(screen.queryByText("0 workers · 0 active · 0 queued")).not.toBeInTheDocument();
    complete({apply_state: "applied", executor_state: "running", worker_count: 1, active_jobs: 1,
      queued_jobs: 1, applied_cpu_limit: 1, detail: "Running 1 PDF workers on 1 CPUs."});
    await screen.findByText("1 workers · 1 active · 1 queued", {}, {timeout: 2000});
    expect(screen.queryByText("Worker counts unavailable.")).not.toBeInTheDocument();
  });

  it("keeps unsupported activation disabled and explains host capability", async () => {
    const {api} = managedClient({...disabled, activation_supported: false, activation_unavailable_reason: "Linux CPU affinity is unavailable."});
    render(<HostCpuBudgetSettings api={api} />);
    expect(await screen.findByLabelText("Enable background PDF workers")).toBeDisabled();
    expect(screen.getByText("Linux CPU affinity is unavailable.")).toBeVisible();
    expect(screen.getByText(/Chat runtimes, external tools and GPUs/)).toBeVisible();
  });

  it("can disable a saved enabled policy when activation support is unavailable", async () => {
    const user = userEvent.setup(); const {api, request} = managedClient({...disabled, enabled: true,
      activation_supported: false, activation_unavailable_reason: "Linux CPU affinity is unavailable.",
      apply_state: "failed", detail: "The saved workers could not start."});
    render(<HostCpuBudgetSettings api={api} />);
    const toggle = await screen.findByLabelText("Enable background PDF workers");
    expect(toggle).toBeEnabled();
    await user.click(toggle); await user.click(screen.getByRole("button", {name: "Save CPU budget"}));
    await waitFor(() => expect(request.mock.calls.some(([, o]) => o?.method === "PUT")).toBe(true));
    expect(JSON.parse(String(request.mock.calls.find(([, o]) => o?.method === "PUT")?.[1]?.body)).enabled).toBe(false);
  });

  it("requires whole worker CPUs and does not enable workers on a budget-only save", async () => {
    const user = userEvent.setup(); const {api, request} = managedClient();
    render(<HostCpuBudgetSettings api={api} />);
    await screen.findByLabelText("Enable background PDF workers");
    await user.selectOptions(screen.getByLabelText("CPU budget mode"), "custom");
    const limit = screen.getByLabelText("Maximum background CPUs");
    await user.clear(limit); await user.type(limit, "1.5");
    expect(screen.getByRole("button", {name: "Save CPU budget"})).toBeDisabled();
    await user.clear(limit); await user.type(limit, "2");
    await user.click(screen.getByRole("button", {name: "Save CPU budget"}));
    await waitFor(() => expect(request.mock.calls.some(([, o]) => o?.method === "PUT")).toBe(true));
    expect(JSON.parse(String(request.mock.calls.find(([, o]) => o?.method === "PUT")?.[1]?.body))).toMatchObject({enabled: false, cpu_limit: 2});
  });

  it("retries a failed activation with the latest saved revision", async () => {
    const user = userEvent.setup(); const {api, request, complete} = managedClient();
    render(<HostCpuBudgetSettings api={api} />);
    await user.click(await screen.findByLabelText("Enable background PDF workers"));
    await user.click(screen.getByRole("button", {name: "Save CPU budget"}));
    await screen.findByText("Starting background PDF workers…");
    complete({apply_state: "failed", executor_state: "failed", detail: "Background PDF workers could not apply the setting. Save again to retry."});
    await screen.findByText(/could not apply the setting/, {}, {timeout: 2000});
    await user.click(screen.getByRole("button", {name: "Save CPU budget"}));
    await screen.findByText("Starting background PDF workers…");
    const writes = request.mock.calls.filter(([, o]) => o?.method === "PUT");
    expect(JSON.parse(String(writes[1][1]?.body))).toMatchObject({enabled: true, expected_revision: 2});
    complete({apply_state: "applied", executor_state: "running", worker_count: 2, applied_cpu_limit: 2, detail: "Running 2 PDF workers on 2 CPUs."});
    await screen.findByText("Running 2 PDF workers on 2 CPUs.", {}, {timeout: 2000});
  });

  it("resets activation drafts when the selected Core host changes", async () => {
    const user = userEvent.setup(); const a = managedClient(); const b = managedClient({...disabled, host_id: "host-b", host_name: "Synthetic host B"});
    const view = render(<HostCpuBudgetSettings api={a.api} />);
    await user.click(await screen.findByLabelText("Enable background PDF workers"));
    expect(screen.getByLabelText("Enable background PDF workers")).toBeChecked();
    view.rerender(<HostCpuBudgetSettings api={b.api} />);
    await screen.findByText(/Synthetic host B/);
    expect(screen.getByLabelText("Enable background PDF workers")).not.toBeChecked();
    expect(a.request.mock.calls.some(([, o]) => o?.method === "PUT")).toBe(false);
  });
});
