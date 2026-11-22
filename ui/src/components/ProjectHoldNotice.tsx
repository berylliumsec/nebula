import { CirclePause, CirclePlay, LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";
import type { ApiClient } from "../api/client";
import type { ProjectHoldState } from "../api/projectHold";
import { useProjectHold } from "../hooks/useProjectHold";
import { useConfirmation } from "./DialogSystem";

function goals(count: number): string {
  return count === 1 ? "1 goal" : `${count} goals`;
}

/** Whole seconds until Core stops what is still running, or undefined. */
function useGraceRemaining(hold: ProjectHoldState): number | undefined {
  const [now, setNow] = useState(() => Date.now());
  const until = hold.phase === "holding" && hold.graceUntil ? Date.parse(hold.graceUntil) : NaN;
  useEffect(() => {
    if (Number.isNaN(until)) return;
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, [until]);
  return Number.isNaN(until) ? undefined : Math.max(0, Math.ceil((until - now) / 1_000));
}

function duration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes}m ${String(seconds % 60).padStart(2, "0")}s` : `${seconds}s`;
}

/**
 * One calm status line while Core holds this project for an update: no chat
 * work starts, running responses get a grace period, and the goals the hold
 * paused resume on their own when it is released. Never an alert.
 */
export function ProjectHoldNotice({ api, projectId }: { api?: ApiClient; projectId?: string }) {
  const hold = useProjectHold(api, projectId);
  const remaining = useGraceRemaining(hold);
  const confirm = useConfirmation();
  const [releasing, setReleasing] = useState(false);
  const [error, setError] = useState<string>();
  if (!hold.held || !projectId) return null;
  const resumes = hold.pausedGoals
    ? `${goals(hold.pausedGoals)} resume on their own`
    : "Paused work resumes on its own";
  if (hold.phase === "holding") {
    const grace = remaining === undefined
      ? "Running responses can finish, then they stop."
      : remaining > 0
        ? `Running responses can finish for ${duration(remaining)}, then they stop.`
        : "Running responses are being stopped.";
    return <div className="workspace-state-banner project-runtime-notice project-hold-notice" role="status">
      <LoaderCircle className="spin" size={16} aria-hidden="true" />
      <span><strong>Pausing work in this project</strong><small>Core is being updated. {grace} {resumes} afterwards.</small></span>
    </div>;
  }
  const release = async () => {
    if (!api || !hold.holdId) return;
    if (!await confirm({
      title: "Release the hold?",
      message: `${hold.pausedGoals ? `The ${goals(hold.pausedGoals)} it paused resume now.` : "Paused work resumes now."} Release only if the update that placed it is no longer running.${hold.reason ? ` Placed for: ${hold.reason}.` : ""}`,
      confirmLabel: "Release",
      cancelLabel: "Keep paused",
    })) return;
    setReleasing(true);
    setError(undefined);
    try {
      await api.projectHolds.release(projectId, hold.holdId);
    } catch (caught) {
      // diagnostic-expected: the request path logs it; the banner says the release did not happen.
      setError(caught instanceof Error ? caught.message : "The hold could not be released.");
    } finally {
      setReleasing(false);
    }
  };
  return <div className="workspace-state-banner project-runtime-notice project-hold-notice held" role="status">
    <CirclePause size={16} aria-hidden="true" />
    <span><strong>Work in this project is paused</strong><small>Core is being updated. {hold.pausedGoals ? `${goals(hold.pausedGoals)} and queued messages resume on their own` : "Queued work resumes on its own"} when it finishes. Saved work is safe.</small>{error && <small className="project-hold-error" role="alert">{error}</small>}</span>
    <button className="button quiet project-hold-release" type="button" aria-label="Release hold" title="Release hold" disabled={releasing} onClick={() => void release()}>
      <CirclePlay size={14} aria-hidden="true" /><span className="project-hold-release-label">Release</span>
    </button>
  </div>;
}
