import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApplicationModelGraph } from "./ApplicationModelGraph";

const data = {
  states: [{ id: "state-1", branch_key: "tab-a", parent_state_ids: [], object_version_ids: ["version-1"], observation_ids: ["observation-1"] }],
  observations: [{ id: "observation-1", source_kind: "browser_traffic", source_id: "exchange-1", facts: { route: { kind: "concrete", type: "string", value: "https://example.test/account" }, status_code: { kind: "concrete", type: "integer", value: 200 } } }],
  objects: [{ id: "object-1", label: "/account · id 42" }],
  object_versions: [{ id: "version-1", object_id: "object-1", properties: { status: { kind: "concrete", type: "string", value: "active" }, token: { kind: "unknown", type: "string", reason: "redacted" } } }],
  assertions: [{ id: "assertion-1", subject: "object-1", predicate: "session issuer exists", support: "inferred", lifecycle: "proposed" }],
};

describe("ApplicationModelGraph", () => {
  it("renders the observed-to-inferred topology and exposes keyboard buttons", () => {
    const onSelectObject = vi.fn();
    render(<ApplicationModelGraph data={data} selectedStateId="state-1" onSelectState={vi.fn()} onSelectObject={onSelectObject} />);
    expect(screen.getByLabelText("Application model graph")).toBeVisible();
    expect(screen.getByText("/account · 200")).toBeVisible();
    expect(screen.getByText("session issuer exists")).toBeVisible();
    const object = screen.getByRole("button", { name: /account · id 42/i });
    fireEvent.click(object);
    expect(onSelectObject).toHaveBeenCalledWith("object-1");
  });

  it("explains the empty state without inventing nodes", () => {
    render(<ApplicationModelGraph data={{ states: [], observations: [], objects: [], object_versions: [], assertions: [] }} onSelectState={vi.fn()} onSelectObject={vi.fn()} />);
    expect(screen.getByText("No model map yet")).toBeVisible();
  });
});
