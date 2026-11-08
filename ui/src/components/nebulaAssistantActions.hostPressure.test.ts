import {expect, it} from "vitest";
import type {ChatStreamEvent} from "../api/types";
import {settingClientAction} from "./nebulaAssistantActions";
it("refreshes pressure settings only for verified selected-project writes", () => {
  const event: ChatStreamEvent = {type: "tool_completed", turnId: "turn", toolCallId: "call", capability: "nebula.capabilities.change", status: "complete", summary: "Saved", evidenceIds: [], artifacts: [], step: 0,
    receipt: {state: "core_settings_saved", read_back: true, butler_project_id: "selected", butler_operation: "nebula.settings.api_request", client_action: {kind: "host_pressure_refresh", project_id: "selected"}}};
  expect(settingClientAction(event, "selected")).toEqual({kind: "host_pressure_refresh", projectId: "selected"});
  expect(settingClientAction(event, "other")).toBeUndefined();
  expect(settingClientAction({...event, receipt: {...event.receipt, read_back: false}}, "selected")).toBeUndefined();
});
