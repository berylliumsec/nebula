export type AvailabilityState = "available" | "checking" | "blocked" | "failed";

export interface RecoveryAction {
  id: string;
  label: string;
  destination?: string;
}

export interface FeatureAvailability {
  state: AvailabilityState;
  reasonCode?: string;
  detail: string;
  recovery?: RecoveryAction;
  error?: unknown;
}

export function projectAvailability(projectId?: string): FeatureAvailability {
  return projectId
    ? { state: "available", detail: "Project is ready." }
    : {
        state: "blocked",
        reasonCode: "project_required",
        detail: "Create or select a Project to use this feature.",
        recovery: { id: "create_project", label: "Create a Project" },
      };
}

export function terminalAvailability(
  projectId: string | undefined,
  status: "detecting_runner" | "needs_runner" | "preparing_image" | "ready" | "disabled" | "error" | undefined,
  detail?: string,
): FeatureAvailability {
  if (!projectId) return projectAvailability(projectId);
  if (status === "ready") return { state: "available", detail: detail ?? "Terminal is ready." };
  if (status === "detecting_runner" || status === "preparing_image" || status === undefined) {
    return { state: "checking", reasonCode: status ?? "setup_loading", detail: detail ?? "Checking Terminal setup." };
  }
  return {
    state: status === "error" ? "failed" : "blocked",
    reasonCode: status ?? "terminal_unavailable",
    detail: detail ?? "Terminal needs a supported local Docker or Podman runtime.",
    recovery: { id: "open_setup", label: "Open Terminal setup", destination: "/settings#setup-settings" },
  };
}
