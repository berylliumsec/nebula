import { CirclePause, LoaderCircle } from "lucide-react";
import type { ApiClient } from "../api/client";
import { useProjectRuntimeState } from "../hooks/useProjectRuntime";

/**
 * One calm status line while open views wait for their project's worker.
 * An idle project's worker retires and is readmitted when the operator acts
 * again; nothing failed, so this is never an alert and never blocks navigation.
 */
export function ProjectRuntimeNotice({ api }: { api?: ApiClient }) {
  const state = useProjectRuntimeState(api);
  if (state === "available") return null;
  // Reads never wake an idle project: say what will, instead of promising a refresh.
  if (state === "idle") return <div className="workspace-state-banner project-runtime-notice" role="status">
    <CirclePause size={16} aria-hidden="true" />
    <span><strong>This project is idle</strong><small>Saved work is safe. Its views load when you resume work here, for example by continuing a conversation.</small></span>
  </div>;
  return <div className="workspace-state-banner project-runtime-notice" role="status">
    <LoaderCircle className="spin" size={16} aria-hidden="true" />
    <span><strong>Reconnecting to this project</strong><small>Saved work is safe. This view refreshes when the project is ready.</small></span>
  </div>;
}
