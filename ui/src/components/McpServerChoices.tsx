import { useEffect, useState } from "react";
import { Ban } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { McpServerProfile } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";

export type McpServerState = "always" | "on-demand" | "not-sent";

const STATE_LABELS: Record<McpServerState, string> = {
  always: "Always loaded",
  "on-demand": "On demand",
  "not-sent": "Not sent",
};

interface McpServerChoicesProps {
  api?: Pick<ApiClient, "getEngagementScope">;
  projectId?: string;
  servers: McpServerProfile[];
  selectedIds: string[];
  excludedIds?: string[];
  contextMode?: "on_demand" | "always_loaded";
  onContextModeChange?: (mode: "on_demand" | "always_loaded") => void;
  disabled: boolean;
  onChange: (nextIds: string[]) => void;
  onExclude?: (id: string) => void;
}

/** Provider-chat MCP access and the next turn's schema-loading policy. */
export function McpServerChoices({ api, projectId, servers, selectedIds, excludedIds = [], contextMode = "always_loaded", onContextModeChange, disabled, onChange, onExclude }: McpServerChoicesProps) {
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
      {onContextModeChange && <select className="chat-mcp-context" aria-label="MCP tool context" value={contextMode} disabled={disabled} onChange={(event) => onContextModeChange(event.target.value as "on_demand" | "always_loaded")}>
        <option value="on_demand">Load schemas on demand</option>
        <option value="always_loaded">Always load selected schemas</option>
      </select>}
      {servers.length > 0 && <small className="chat-mcp-hint">{contextMode === "on_demand"
        ? "Brief tool directory only. Load schemas when needed this turn; unchecking excludes the server next turn."
        : onDemand
        ? "Checked servers are sent every turn. Unchecking excludes all their tools next turn; untouched servers stay on demand."
        : "On-demand tools are off for this project, so only checked servers are sent."}</small>}
      {servers.length ? servers.map((server) => {
        const selected = selectedIds.includes(server.id);
        const excluded = excludedIds.includes(server.id);
        const state: McpServerState = excluded ? "not-sent" : selected ? contextMode === "on_demand" ? "on-demand" : "always" : server.projectId ? "not-sent" : contextMode === "on_demand" || onDemand ? "on-demand" : "not-sent";
        return (
          <div className="chat-mcp-server-row" key={server.id}>
          <label className="chat-knowledge-toggle chat-mcp-choice" data-state={state} key={server.id}>
            <input type="checkbox" checked={selected} disabled={disabled} onChange={(event) => onChange(event.target.checked ? [...selectedIds, server.id] : selectedIds.filter((id) => id !== server.id))} />
            <span>{server.name}<small>{server.tools.length} tools · <span className="chat-mcp-state">{excluded ? "Excluded · Not sent next turn" : server.projectId && !selected ? "Select to grant this assistant" : STATE_LABELS[state]}</span></small></span>
          </label>
          {state === "on-demand" && !selected && onExclude && <button className="button ghost" type="button" aria-label={`Exclude ${server.name} next turn`} title={`Exclude all ${server.name} tools next turn`} disabled={disabled} onClick={() => onExclude(server.id)}><Ban size={16} aria-hidden="true" /></button>}
          </div>
        );
      }) : <small>No enabled MCP profiles</small>}
    </div>
  );
}
