import {cleanup, fireEvent, render, screen, waitFor} from "@testing-library/react";
import {afterEach, describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import type {HostPressure} from "../api/hostPressure";
import {HostPressureSettings} from "./HostPressureSettings";

const initial: HostPressure = {host_id: "host-a", host_name: "Synthetic", revision: 1, saved_thresholds: {cpu: .8, memory: .8, load: .8, io: .8}, effective_thresholds: {cpu: .8, memory: .8, load: .8, io: .8}, readings: {cpu: .85, memory: .4, load: null, io: .1}, waiting: true, pressure_metric: "cpu", reason: "Waiting for host pressure: CPU use", concurrency: {provider_limit: 60, background_limit: 58, active: 0, active_background: 0}};
afterEach(cleanup);
function client() {
  let state = structuredClone(initial);
  const request = vi.fn(async (_path, options) => {
    if (options?.method === "PUT") {
      const body = JSON.parse(options.body);
      state = {...state, revision: state.revision + 1, saved_thresholds: body.thresholds, effective_thresholds: body.thresholds};
    }
    return structuredClone(state);
  });
  return {api: {request} as unknown as ApiClient, request};
}
describe("host pressure", () => {
  it("shows readings and effective limits and saves with authoritative readback", async () => {
    const {api, request} = client(); render(<HostPressureSettings api={api} />);
    const cpu = await screen.findByLabelText("CPU use threshold (%)");
    expect(cpu).toHaveValue(80);
    expect(screen.getByText("Provider limit: 60 · Background limit: 58")).toBeVisible();
    expect(screen.getByText(/Current: Unknown/)).toBeVisible();
    fireEvent.change(cpu, {target: {value: "90"}});
    fireEvent.click(screen.getByRole("button", {name: "Save pressure thresholds"}));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(3));
    expect(JSON.parse(request.mock.calls[1][1].body)).toMatchObject({host_id: "host-a", expected_revision: 1, thresholds: {cpu: .9}});
    await waitFor(() => expect(screen.getByRole("button", {name: "Save pressure thresholds"})).toBeDisabled());
  });
  it("validates thresholds and preserves the draft when saving fails", async () => {
    const {api, request} = client(); render(<HostPressureSettings api={api} />);
    const cpu = await screen.findByLabelText("CPU use threshold (%)");
    fireEvent.change(cpu, {target: {value: "0"}});
    expect(screen.getByRole("button", {name: "Save pressure thresholds"})).toBeDisabled();
    fireEvent.change(cpu, {target: {value: "90"}});
    request.mockRejectedValueOnce(new Error("409 reload settings"));
    fireEvent.click(screen.getByRole("button", {name: "Save pressure thresholds"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("409");
    expect(cpu).toHaveValue(90);
  });
  it("does not treat polling during its own save as an external conflict", async () => {
    let state = structuredClone(initial);
    let finish!: () => void;
    const request = vi.fn(async (_path, options) => {
      if (options?.method === "PUT") {
        const body = JSON.parse(options.body);
        state = {...state, revision: 2, saved_thresholds: body.thresholds, effective_thresholds: body.thresholds};
        await new Promise<void>(resolve => {finish = resolve;});
      }
      return structuredClone(state);
    });
    render(<HostPressureSettings api={{request} as unknown as ApiClient} />);
    const cpu = await screen.findByLabelText("CPU use threshold (%)");
    fireEvent.change(cpu, {target: {value: "90"}});
    fireEvent.click(screen.getByRole("button", {name: "Save pressure thresholds"}));
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    await new Promise(resolve => setTimeout(resolve, 2100));
    finish();
    await waitFor(() => expect(screen.getByRole("button", {name: "Save pressure thresholds"})).toBeDisabled());
    fireEvent.change(cpu, {target: {value: "95"}});
    await waitFor(() => expect(screen.getByRole("button", {name: "Save pressure thresholds"})).toBeEnabled());
  });

});
