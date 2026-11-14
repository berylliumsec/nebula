import { LoaderCircle } from "lucide-react";
import type { ApiClient } from "../api/client";
import { useProjectRuntimeState } from "../hooks/useProjectRuntime";

/**
 * One calm status line while open views wait for their project's worker.
 * An idle project's worker retires and is readmitted when the operator acts
 * again; nothing failed, so this is never an alert and never blocks navigation.
 */
export function ProjectRuntimeNotice({ api }: { api?: ApiClient }) {
  const state = useProjectRuntimeState(api);
  if (state !== "reconnecting") return null;
  return <div className="workspace-state-banner project-runtime-notice" role="status">
    <LoaderCircle className="spin" size={16} aria-hidden="true" />
    <span><strong>Reconnecting to this project</strong><small>Saved work is safe. This view refreshes when the project is ready.</small></span>
  </div>;
}
