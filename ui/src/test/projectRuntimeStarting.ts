import { vi } from "vitest";
import { ApiError } from "../api/client";
import { PROJECT_RUNTIME_UNAVAILABLE, ProjectRuntimeRecovery, type ProjectRuntimeReason } from "../api/projectRuntime";

/**
 * A project whose worker is starting, as a view sees it: each read answers the
 * coded 503 (which the real ApiClient records with the shared recovery), and
 * the recovery's probe finds the worker answering on its first try (after its
 * one-second backoff).
 */
export function startingProject() {
  const probe = vi.fn().mockResolvedValue({ answered: true });
  const projectRuntime = new ProjectRuntimeRecovery(probe);
  const unavailable = (path: string, reason: ProjectRuntimeReason = "starting") => {
    projectRuntime.unavailable(path, undefined, reason);
    return new ApiError("The project runtime is not ready.", 503, undefined, {
      detail: "The project runtime is not ready. Saved work is unchanged; read again shortly.",
      code: PROJECT_RUNTIME_UNAVAILABLE, reason, retryable: true,
    });
  };
  return { projectRuntime, probe, unavailable };
}
