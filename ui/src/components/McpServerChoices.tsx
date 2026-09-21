import { useEffect, useState } from "react";
import type { ApiClient } from "../api/client";
import type { McpServerProfile } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";

export type McpServerState = "always" | "on-demand" | "not-sent";

const STATE_LABELS: Record<McpServerState, string> = {
  always: "Always sent",
  "on-demand": "On demand",
  "not-sent": "Not sent",
};

interface McpServerChoicesProps {
  api?: Pick<ApiClient, "getEngagementScope">;
  projectId?: string;
  servers: McpServerProfile[];
  selectedIds: string[];
  disabled: boolean;
  onChange: (nextIds: string[]) => void;
}

/** Provider-chat MCP servers: ticked ones go with every message, the rest on demand. */
export function McpServerChoices({ api, projectId, servers, selectedIds, disabled, onChange }: McpServerChoicesProps) {
  // Core defers tools unless the project turned that off, so assume on demand
  // until the project's scope says otherwise.
  const [onDemand, setOnDemand] = useState(true);
  useEffect(() => {
    setOnDemand(true);
    if (!api || !projectId) return;
    let active = true;
    void api.getEngagementScope(projectId).then((scope) => {
      // Opting into Jev keeps deferral on even when on-demand loading is off.
      if (active) setOnDemand(scope.onDemandTools !== false || scope.toolSuggestions);
    }).catch((caughtError) => {
      void logCaughtDiagnostic("interface.sessions.mcp_scope_load_failed", "Project scope could not be loaded for MCP servers.", caughtError, "assistant_settings");
    });
    return () => { active = false; };
  }, [api, projectId]);

  return (
    // The empty state keeps the compact one-line layout.
    <div className={`chat-harness-mcp${servers.length ? " chat-mcp-choices" : ""}`} data-guide="mcp-turn">
      <span>MCP servers</span>
      {servers.length > 0 && <small className="chat-mcp-hint">{onDemand
        ? "Checked servers are sent with every message. Nebula loads tools from the rest only when a message needs them."
        : "On-demand tools are off for this project, so only checked servers are sent."}</small>}
      {servers.length ? servers.map((server) => {
        const selected = selectedIds.includes(server.id);
        const state: McpServerState = selected ? "always" : onDemand ? "on-demand" : "not-sent";
        return (
          <label className="chat-knowledge-toggle chat-mcp-choice" data-state={state} key={server.id}>
            <input type="checkbox" checked={selected} disabled={disabled} onChange={(event) => onChange(event.target.checked ? [...selectedIds, server.id] : selectedIds.filter((id) => id !== server.id))} />
            <span>{server.name}<small>{server.tools.length} tools · <span className="chat-mcp-state">{STATE_LABELS[state]}</span></small></span>
          </label>
        );
      }) : <small>No enabled MCP profiles</small>}
    </div>
  );
}
