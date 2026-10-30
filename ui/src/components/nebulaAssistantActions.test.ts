import { describe, expect, it } from "vitest";
import type { ChatStreamEvent, HarnessProfile } from "../api/types";
import { assistantTurnOptions, assistantWorkReceipt, headlessSessionChange, isSettingNavigationRequest, isThemeChangeCommand, projectModeChange, projectScopeChange, settingClientAction, settingToOpen, themeChange } from "./nebulaAssistantActions";

describe("settings assistant actions", () => {
  it("maps explicit device-theme commands to bounded values without taking over questions", () => {
    expect(themeChange("change to zero theme")).toBe("zero-dark");
    expect(themeChange("set the theme to Zero Light")).toBe("zero-light");
    expect(themeChange("switch my appearance to Studio Dark")).toBe("studio-dark");
    expect(themeChange("use dark theme")).toBe("dark");
    expect(themeChange("set theme to light")).toBe("light");
    expect(themeChange("set theme to Glacier")).toBe("glacier");
    expect(themeChange("switch to Amber")).toBe("amber");
    expect(themeChange("change to Zero Classic")).toBe("zero-classic");
    expect(themeChange("change it to the light theme")).toBe("light");
    expect(themeChange("could you switch this device to the Light theme")).toBe("light");
    expect(themeChange("change it to light")).toBe("light");
    expect(isThemeChangeCommand("change it to the rainbow theme")).toBe(true);
    expect(themeChange("change it to the rainbow theme")).toBeUndefined();
    expect(isThemeChangeCommand("how do I change to zero theme?")).toBe(false);
    expect(themeChange("how do I change to zero theme?")).toBeUndefined();
    expect(isThemeChangeCommand("could you make the screen look like Nebula Zero?")).toBe(false);
    expect(themeChange("could you make the screen look like Nebula Zero?")).toBeUndefined();
    expect(themeChange("open the theme setting")).toBeUndefined();
    expect(themeChange("change project execution mode to host")).toBeUndefined();
  });
  it("accepts only validated browser actions from scoped settings-tool receipts", () => {
    const event: ChatStreamEvent = {
      type: "tool_completed", turnId: "turn", toolCallId: "call", capability: "nebula.settings.set_device_theme",
      status: "complete", summary: "Action requested", evidenceIds: [], artifacts: [], step: 0,
      receipt: { state: "pending_browser_apply", client_action: { kind: "device_theme", theme: "zero-dark" } },
    };
    expect(settingClientAction(event)).toEqual({ kind: "device_theme", theme: "zero-dark" });
    for (const theme of ["glacier", "amber", "zero-classic"]) {
      expect(settingClientAction({ ...event, receipt: { state: "pending_browser_apply", client_action: { kind: "device_theme", theme } } }))
        .toEqual({ kind: "device_theme", theme });
    }
    expect(settingClientAction({ ...event, capability: "nebula_settings_open_1234567890abcdef",
      receipt: { state: "pending_browser_apply", client_action: { kind: "open_setting", setting_id: "appearance" } } }))
      .toMatchObject({ kind: "open_setting", setting: { id: "settings.appearance" } });
    expect(settingClientAction({ ...event, capability: "nebula.settings.open",
      receipt: { state: "pending_browser_apply", client_action: { kind: "open_setting", setting_id: "butler-effort" } } }))
      .toMatchObject({ kind: "open_setting", setting: { id: "settings.butler-effort", target: "butler-settings" } });
    expect(settingClientAction({ ...event, capability: "nebula.settings.open",
      receipt: { state: "pending_browser_apply", client_action: { kind: "open_setting", setting_id: "butler-voice" } } }))
      .toMatchObject({ kind: "open_setting", setting: { id: "settings.butler-voice" } });
    expect(settingClientAction({ ...event, capability: "nebula.settings.open",
      receipt: { state: "pending_browser_apply", client_action: { kind: "open_setting", setting_id: "butler-instructions" } } }))
      .toMatchObject({ kind: "open_setting", setting: { id: "settings.butler-instructions", target: "butler-instructions-settings", description: expect.stringContaining("directly in the Markdown code editor") } });
    expect(settingClientAction({ ...event, capability: "nebula.settings.open",
      receipt: { state: "pending_browser_apply", client_action: { kind: "open_setting", setting_id: "butler-activity" } } }))
      .toMatchObject({ kind: "open_setting", setting: { id: "settings.butler-activity", target: "butler-oversight" } });
    expect(settingClientAction({ ...event, capability: "nebula.navigate.open",
      receipt: { state: "pending_browser_apply", client_action: { kind: "navigate", destination_id: "projects" } } }))
      .toMatchObject({ kind: "navigate", destination: { id: "projects" } });
    expect(settingClientAction({ ...event, capability: "nebula.navigate.open",
      receipt: { state: "pending_browser_apply", client_action: { kind: "navigate", destination_id: "notebook" } } }))
      .toMatchObject({ kind: "navigate", destination: { path: "/projects/{project_id}/workbench?view=notebook" } });
    expect(settingClientAction({ ...event, capability: "nebula_navigate_open_1234567890abcdef",
      receipt: { state: "pending_browser_apply", client_action: { kind: "navigate", destination_id: "unlisted" } } })).toBeUndefined();
    expect(settingClientAction({ ...event, capability: "other.tool" })).toBeUndefined();
    expect(settingClientAction({ ...event, capability: "nebula_settings_set_project_mode_1234567890abcdef",
      receipt: { state: "pending_browser_apply", client_action: { kind: "project_execution_mode", mode: "host", from: "docker" } } }))
      .toEqual({ kind: "project_execution_mode", mode: "host", from: "docker" });
    expect(settingClientAction({ ...event, capability: "nebula.settings.set_project_scope",
      receipt: { state: "pending_browser_apply", client_action: { kind: "project_scope", changes: { mode: "all_destinations" } } } }))
      .toEqual({ kind: "project_scope", changes: { mode: "all_destinations" } });
    expect(settingClientAction({ ...event, capability: "nebula.settings.headless_session",
      receipt: { state: "pending_browser_apply", client_action: { kind: "headless_session", action: "start" } } }))
      .toEqual({ kind: "headless_session", action: "start" });
    expect(settingClientAction({ ...event, capability: "nebula.settings.set_project_scope",
      receipt: { state: "pending_browser_apply", client_action: { kind: "project_scope", changes: { mode: "allow_all", unknown: true } } } })).toBeUndefined();
    expect(settingClientAction({ ...event, receipt: { state: "pending_browser_apply", client_action: { kind: "device_theme", theme: "unlisted" } } })).toBeUndefined();
  });
  it("uses the gateway's exact operation and selected-project receipt for work and browser actions", () => {
    const missionId = "70d5a305-6f30-4270-b27d-2322d7385fbf";
    const base: ChatStreamEvent = {
      type: "tool_completed", turnId: "turn", toolCallId: "call", capability: "nebula.capabilities.change",
      status: "complete", summary: "Saved", evidenceIds: [], artifacts: [], step: 0,
      receipt: {butler_operation: "nebula.work.start_mission", butler_project_id: "selected", run_id: missionId},
    };
    expect(assistantWorkReceipt(base, "selected")?.url).toBe(`/projects/selected/workbench?view=missions&mission=${missionId}`);
    expect(assistantWorkReceipt(base, "other")).toBeUndefined();
    const browser = {...base, receipt: {
      butler_operation: "nebula.settings.open", butler_project_id: "selected",
      state: "pending_browser_apply", client_action: {kind: "open_setting", setting_id: "appearance"},
    }};
    expect(settingClientAction(browser, "selected")).toMatchObject({kind: "open_setting", setting: {id: "settings.appearance"}});
    expect(settingClientAction(browser, "other")).toBeUndefined();
    expect(settingClientAction({...browser, receipt: {...browser.receipt, butler_operation: "nebula.work.start_chat"}}, "selected")).toBeUndefined();
  });
  it("accepts only scoped, typed project lifecycle handoffs", () => {
    const projectId = "70d5a305-6f30-4270-b27d-2322d7385fbf";
    const event: ChatStreamEvent = {
      type: "tool_completed", turnId: "turn", toolCallId: "project-call", capability: "nebula.capabilities.change",
      status: "complete", summary: "Pending on browser", evidenceIds: [], artifacts: [], step: 0,
      receipt: { butler_operation: "nebula.projects.archive", butler_project_id: "selected",
        state: "pending_browser_apply", client_action: {kind: "project_archive", project_id: projectId, name: "Fixture", expected_revision: 3} },
    };
    expect(settingClientAction(event, "selected")).toEqual({kind: "project_archive", projectId, name: "Fixture", expectedRevision: 3});
    expect(settingClientAction(event, "other")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {kind: "project_archive", project_id: "../other", name: "Fixture", expected_revision: 3}}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {kind: "project_archive", project_id: projectId, name: "Fixture", expected_revision: 0}}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.projects.restore"}}, "selected")).toBeUndefined();
    const requestId = "70790d17-cd62-450a-b73b-28684e410300";
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.projects.create", client_action: {kind: "project_create", name: "New", description: "", client_name: "", request_id: requestId}}}, "selected"))
      .toEqual({kind: "project_create", name: "New", description: "", clientName: "", requestId});
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.projects.create", client_action: {kind: "project_create", name: "New", description: "", client_name: ""}}}, "selected")).toBeUndefined();
    const linked = {kind: "project_create_linked", name: "Linked", description: "", client_name: "", request_id: requestId};
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.projects.create_linked", client_action: linked}}, "selected"))
      .toEqual({kind: "project_create_linked", sourceProjectId: "selected", name: "Linked", description: "", clientName: "", requestId});
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.projects.create_linked", client_action: {...linked, workspace_path: "/private"}}}, "selected"))
      .toEqual({kind: "project_create_linked", sourceProjectId: "selected", name: "Linked", description: "", clientName: "", requestId});
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.projects.create", client_action: linked}}, "selected")).toBeUndefined();
  });
  it("accepts only a scoped transcript export handoff from Core", () => {
    const sessionId = "9482d86e-2ddc-436b-90be-30b878f68537";
    const event: ChatStreamEvent = {
      type: "tool_completed", turnId: "turn", toolCallId: "export-call", capability: "nebula.capabilities.change",
      status: "complete", summary: "Pending on browser", evidenceIds: [], artifacts: [], step: 0,
      receipt: {butler_operation: "nebula.chats.export", butler_project_id: "selected",
        state: "pending_browser_apply", client_action: {kind: "chat_export", project_id: "selected", session_id: sessionId, expected_revision: 5}},
    };
    expect(settingClientAction(event, "selected")).toEqual({kind: "chat_export", projectId: "selected", sessionId, expectedRevision: 5});
    expect(settingClientAction(event, "other")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.work.chat"}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {kind: "chat_export", project_id: "other", session_id: sessionId, expected_revision: 5}}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {kind: "chat_export", project_id: "selected", session_id: "../other", expected_revision: 5}}}, "selected")).toBeUndefined();
  });
  it("accepts only scoped report drafts, current revisions and device handoffs", () => {
    const reportId = "9482d86e-2ddc-436b-90be-30b878f68537";
    const findingId = "70d5a305-6f30-4270-b27d-2322d7385fbf";
    const createAction = {kind: "report_create", project_id: "selected", report_id: reportId,
      title: "Assessment", executive_summary: "Summary", finding_ids: [findingId], observation_ids: []};
    const event: ChatStreamEvent = {
      type: "tool_completed", turnId: "turn", toolCallId: "report-call", capability: "nebula.capabilities.change",
      status: "complete", summary: "Pending browser action", evidenceIds: [], artifacts: [], step: 0,
      receipt: {butler_operation: "nebula.reports.create", butler_project_id: "selected",
        state: "pending_browser_apply", client_action: createAction},
    };
    expect(settingClientAction(event, "selected")).toEqual({kind: "report_create", projectId: "selected",
      reportId, title: "Assessment", executiveSummary: "Summary", findingIds: [findingId], observationIds: []});
    expect(settingClientAction(event, "other")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {...createAction,
      project_id: "other"}}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {...createAction,
      finding_ids: ["../foreign"]}}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.reports.update",
      client_action: {kind: "report_update", project_id: "selected", report_id: reportId,
        expected_revision: 3, status: "review", executive_summary: "Saved"}}}, "selected"))
      .toEqual({kind: "report_update", projectId: "selected", reportId, expectedRevision: 3,
        status: "review", executiveSummary: "Saved"});
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.reports.update",
      client_action: {kind: "report_update", project_id: "selected", report_id: reportId,
        expected_revision: 3, status: "final"}}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.reports.signoff",
      client_action: {kind: "report_signoff", project_id: "selected", report_id: reportId, expected_revision: 3}}}, "selected"))
      .toEqual({kind: "report_signoff", projectId: "selected", reportId, expectedRevision: 3});
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.reports.export_bundle",
      client_action: {kind: "report_export_bundle", project_id: "selected", expected_project_revision: 2}}}, "selected"))
      .toEqual({kind: "report_export_bundle", projectId: "selected", expectedProjectRevision: 2});
  });
  it("accepts only scoped workspace file actions with exact paths and hashes", () => {
    const hash = "a".repeat(64);
    const event: ChatStreamEvent = {
      type: "tool_completed", turnId: "turn", toolCallId: "workspace-call", capability: "nebula.capabilities.change",
      status: "complete", summary: "Pending on browser", evidenceIds: [], artifacts: [], step: 0,
      receipt: {butler_operation: "nebula.workspace.download_file", butler_project_id: "selected",
        state: "pending_browser_apply", client_action: {kind: "workspace_download", project_id: "selected",
          path: "notes/research.txt", expected_sha256: hash, size: 12}},
    };
    expect(settingClientAction(event, "selected")).toEqual({kind: "workspace_download", projectId: "selected",
      path: "notes/research.txt", expectedSha256: hash, size: 12});
    expect(settingClientAction(event, "other")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {
      kind: "workspace_download", project_id: "selected", path: "../escape", expected_sha256: hash, size: 12,
    }}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {
      kind: "workspace_download", project_id: "selected", path: "notes/research.txt", expected_sha256: "bad", size: 12,
    }}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt,
      butler_operation: "nebula.workspace.delete_file", client_action: {kind: "workspace_delete",
        project_id: "selected", path: "notes/research.txt", expected_sha256: hash},
    }}, "selected")).toEqual({kind: "workspace_delete", projectId: "selected",
      path: "notes/research.txt", expectedSha256: hash});
  });

  it("accepts only scoped device uploads and empty-folder deletion receipts", () => {
    const identity = "a".repeat(64);
    const event: ChatStreamEvent = {
      type: "tool_completed", turnId: "turn", toolCallId: "folder-call", capability: "nebula.capabilities.change",
      status: "complete", summary: "Pending on browser", evidenceIds: [], artifacts: [], step: 0,
      receipt: {butler_operation: "nebula.workspace.upload_from_device", butler_project_id: "selected",
        state: "pending_browser_apply", client_action: {kind: "workspace_upload", project_id: "selected",
          directory: "captures", expected_directory_identity: identity}},
    };
    expect(settingClientAction(event, "selected")).toEqual({kind: "workspace_upload", projectId: "selected",
      directory: "captures", expectedDirectoryIdentity: identity});
    expect(settingClientAction(event, "other")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {
      kind: "workspace_upload", project_id: "selected", directory: "../escape", expected_directory_identity: identity,
    }}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {
      kind: "workspace_upload", project_id: "selected", directory: "captures", expected_directory_identity: "bad",
    }}}, "selected")).toBeUndefined();
    const deletion = {...event, receipt: {...event.receipt, butler_operation: "nebula.workspace.delete_directory",
      client_action: {kind: "workspace_delete_directory", project_id: "selected", path: "captures",
        expected_entry_identity: identity}}} as ChatStreamEvent;
    expect(settingClientAction(deletion, "selected")).toEqual({kind: "workspace_delete_directory",
      projectId: "selected", path: "captures", expectedEntryIdentity: identity});
  });

  it("accepts revision-bound Notebook index handoffs without a model-supplied host path", () => {
    const event: ChatStreamEvent = {
      type: "tool_completed", turnId: "turn", toolCallId: "index-call", capability: "nebula.capabilities.change",
      status: "complete", summary: "Pending on browser", evidenceIds: [], artifacts: [], step: 0,
      receipt: {butler_operation: "nebula.notebook.connect_file_index", butler_project_id: "selected",
        state: "pending_browser_apply", client_action: {kind: "notebook_index_connect", project_id: "selected",
          expected_project_revision: 4}},
    };
    expect(settingClientAction(event, "selected")).toEqual({kind: "notebook_index_connect",
      projectId: "selected", expectedProjectRevision: 4});
    expect(settingClientAction(event, "other")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {
      kind: "notebook_index_connect", project_id: "other", expected_project_revision: 4,
    }}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {
      kind: "notebook_index_connect", project_id: "selected", expected_project_revision: 0,
    }}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {
      kind: "notebook_index_connect", project_id: "selected", expected_project_revision: 4, path: "/private/index.db",
    }}}, "selected")).toEqual({kind: "notebook_index_connect", projectId: "selected", expectedProjectRevision: 4});
    const removal: ChatStreamEvent = {...event, receipt: {...event.receipt,
      butler_operation: "nebula.notebook.remove_file_index",
      client_action: {kind: "notebook_index_remove", project_id: "selected", expected_project_revision: 4}}};
    expect(settingClientAction(removal, "selected")).toEqual({kind: "notebook_index_remove",
      projectId: "selected", expectedProjectRevision: 4});
    expect(settingClientAction({...removal, receipt: {...removal.receipt,
      butler_operation: "nebula.notebook.connect_file_index"}}, "selected")).toBeUndefined();
  });

  it("accepts only exact shared Library actions for the requesting project", () => {
    const itemId = "9482d86e-2ddc-436b-90be-30b878f68537";
    const event: ChatStreamEvent = {
      type: "tool_completed", turnId: "turn", toolCallId: "library-call", capability: "nebula.capabilities.change",
      status: "complete", summary: "Pending on browser", evidenceIds: [], artifacts: [], step: 0,
      receipt: {butler_operation: "nebula.library.remove", butler_project_id: "selected",
        state: "pending_browser_apply", client_action: {kind: "library_remove", project_id: "selected", item_id: itemId, expected_revision: 5, name: "Shared note"}},
    };
    expect(settingClientAction(event, "selected")).toEqual({kind: "library_remove", projectId: "selected", itemId, expectedRevision: 5, name: "Shared note"});
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.library.reindex",
      client_action: {kind: "library_reindex", project_id: "selected", item_id: itemId, expected_revision: 5, name: "Shared note"}}}, "selected"))
      .toEqual({kind: "library_reindex", projectId: "selected", itemId, expectedRevision: 5, name: "Shared note"});
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.library.download",
      client_action: {kind: "library_download", project_id: "selected", item_id: itemId, expected_revision: 5, name: "Shared note"}}}, "selected"))
      .toEqual({kind: "library_download", projectId: "selected", itemId, expectedRevision: 5, name: "Shared note"});
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.library.add",
      client_action: {kind: "library_add", project_id: "selected"}}}, "selected"))
      .toEqual({kind: "library_add", projectId: "selected"});
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.library.add",
      client_action: {kind: "library_add", project_id: "other"}}}, "selected")).toBeUndefined();
    expect(settingClientAction(event, "other")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, butler_operation: "nebula.library.reindex"}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {kind: "library_remove", project_id: "other", item_id: itemId, expected_revision: 5, name: "Shared note"}}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {kind: "library_remove", project_id: "selected", item_id: "../other", expected_revision: 5, name: "Shared note"}}}, "selected")).toBeUndefined();
    expect(settingClientAction({...event, receipt: {...event.receipt, client_action: {kind: "library_remove", project_id: "selected", item_id: itemId, expected_revision: 0, name: "Shared note"}}}, "selected")).toBeUndefined();
  });
  it("recognizes explicit project mode changes without treating questions as commands", () => {
    expect(projectModeChange("open project policy and change from dockermode to host mode"))
      .toEqual({ from: "docker", mode: "host" });
    expect(projectModeChange("switch project execution mode to Docker"))
      .toEqual({ mode: "docker" });
    expect(projectModeChange("Where is Host mode?")).toBeUndefined();
    expect(projectModeChange("open project policy")).toBeUndefined();
  });
  it("distinguishes all destinations from permission bypass and starts headless sessions", () => {
    expect(projectScopeChange("set project scope to allow all URLs and all network targets"))
      .toEqual({ mode: "all_destinations" });
    expect(projectScopeChange("allow all URLs and network targets"))
      .toEqual({ mode: "all_destinations" });
    expect(projectScopeChange("set project policy to allow all mode"))
      .toEqual({ mode: "allow_all" });
    expect(projectScopeChange("how do I allow all URLs?"))
      .toBeUndefined();
    expect(headlessSessionChange("configure the headless browser session"))
      .toBe("start");
    expect(headlessSessionChange("set project policy to allow all URLs and network targets, then start the headless browser session"))
      .toBe("start");
    expect(headlessSessionChange("stop the headless browser"))
      .toBe("stop");
    expect(headlessSessionChange("why is my headless browser stopped?"))
      .toBeUndefined();
  });
  it("opens an unambiguous named setting and ignores ordinary questions", () => {
    expect(settingToOpen("Please open MCP servers settings")?.id).toBe("settings.mcp");
    expect(settingToOpen("Please open storage source settings")?.id).toBe("settings.storage");
    expect(settingToOpen("can you bring up model provider settings?")?.id).toBe("settings.providers");
    expect(settingToOpen("open project scope")?.id).toBe("settings.network-scope");
    expect(settingToOpen("open notebook host consent settings")?.id).toBe("settings.network-scope");
    expect(settingToOpen("What can I change in settings?")).toBeUndefined();
    expect(settingToOpen("open assistant")).toBeUndefined();
    expect(isSettingNavigationRequest("Where can I fix a host file permission error in Docker mode?")).toBe(false);
    expect(isSettingNavigationRequest("Open sign-off for the synthetic Butler report.")).toBe(false);
    expect(settingToOpen("Open sign-off for the synthetic Butler report.")).toBeUndefined();
    expect(settingToOpen("Why can't my assistant edit a host file in Docker mode?")).toBeUndefined();
  });

  it("routes plain-language requests for every catalog setting without a model turn", () => {
    const cases = [
      ["open usage limits", "settings.usage"],
      ["open theme", "settings.appearance"],
      ["show provider", "settings.providers"],
      ["bring up playbook", "settings.skills"],
      ["open internet", "settings.web-search"],
      ["open storage sources", "settings.storage"],
      ["show follow up", "settings.follow-up"],
      ["open codex", "settings.harnesses"],
      ["open mcp", "settings.mcp"],
      ["open ssh", "settings.ssh-environments"],
      ["open automation", "settings.automation-runtime"],
      ["open vpn", "settings.vpn-egress"],
      ["open podman", "settings.runners"],
      ["open project scope", "settings.network-scope"],
      ["open identity", "settings.operators"],
      ["open qr", "settings.devices"],
      ["open upgrade", "settings.release"],
      ["open telemetry", "settings.diagnostics"],
      ["open readiness", "settings.setup"],
    ];
    for (const [prompt, expected] of cases) expect(settingToOpen(prompt)?.id, prompt).toBe(expected);
  });

  it("finds the intended setting in conversational navigation requests", () => {
    const cases = [
      ["where can I see usage limits?", "settings.usage"],
      ["where can I change the theme colors?", "settings.appearance"],
      ["which setting holds API keys for models?", "settings.providers"],
      ["where are the shared skills?", "settings.skills"],
      ["where can I find web research?", "settings.web-search"],
      ["where can I connect a storage webhook?", "settings.storage"],
      ["where is automatic follow up?", "settings.follow-up"],
      ["where do I configure the harness?", "settings.harnesses"],
      ["where can I add an MCP server?", "settings.mcp"],
      ["which setting manages remote SSH machines?", "settings.ssh-environments"],
      ["where is the automation runtime?", "settings.automation-runtime"],
      ["where do I upload an OpenVPN tunnel profile?", "settings.vpn-egress"],
      ["where is the Podman runner config?", "settings.runners"],
      ["where is project scope for CIDR ports?", "settings.network-scope"],
      ["where can I set operator identity?", "settings.operators"],
      ["where can I pair my phone?", "settings.devices"],
      ["where are release channel updates?", "settings.release"],
      ["where do I inspect logs?", "settings.diagnostics"],
      ["where can I check Core readiness?", "settings.setup"],
    ];
    for (const [prompt, expected] of cases) expect(settingToOpen(prompt)?.id, prompt).toBe(expected);
  });

  it("chooses advertised low effort and a matching fast harness model", () => {
    const harness = {
      id: "grok", models: ["grok-4.7", "grok-4.7-build-fast"], modelOptions: [
        { model: "grok-4.7", reasoningEfforts: [{ id: "low" }], serviceTiers: [] },
        { model: "grok-4.7-build-fast", reasoningEfforts: [{ id: "low" }], serviceTiers: [] },
      ],
    } as unknown as HarnessProfile;
    expect(assistantTurnOptions({ kind: "harness", id: "grok", model: "grok-4.7" }, [harness])).toEqual({
      model: "grok-4.7-build-fast", harnessReasoningEffort: "low",
    });
  });

  it("keeps the current model when it advertises a fast tier", () => {
    const harness = {
      id: "codex", models: ["model", "model-build-fast"], modelOptions: [
        { model: "model", reasoningEfforts: [{ id: "low" }], serviceTiers: [{ id: "fast" }] },
      ],
    } as unknown as HarnessProfile;
    expect(assistantTurnOptions({ kind: "harness", id: "codex", model: "model" }, [harness])).toEqual({
      model: "model", harnessReasoningEffort: "low", harnessServiceTier: "fast",
    });
  });

  it("requests low effort for providers without a fast-mode selection", () => {
    expect(assistantTurnOptions({ kind: "provider", id: "provider", model: "reasoning" }, []))
      .toEqual({ model: "reasoning", reasoningEffort: "low" });
    expect(assistantTurnOptions({ kind: "provider", id: "provider", model: "plain" }, []))
      .toEqual({ model: "plain", reasoningEffort: "low" });
  });
});
