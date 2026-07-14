import { Download, RefreshCw, RotateCcw, X } from "lucide-react";
import { useReleaseUpdate } from "../state/ReleaseUpdateContext";

export function UpdateBanner() {
  const {
    availableUpdate,
    phase,
    error,
    failure,
    dismissed,
    install,
    restart,
    dismiss,
  } = useReleaseUpdate();

  if (!availableUpdate || dismissed || !["available", "installing", "restart", "error"].includes(phase)) {
    return null;
  }

  const restartReady = phase === "restart" || (phase === "error" && failure === "restart");
  const installing = phase === "installing";
  const failed = phase === "error";
  const title = installing
    ? `Installing Nebula ${availableUpdate.version}…`
    : restartReady
      ? `Nebula ${availableUpdate.version} is ready.`
      : failed
        ? `Nebula ${availableUpdate.version} could not be installed.`
        : `Nebula ${availableUpdate.version} is available.`;
  const detail = failed
    ? error
    : restartReady
      ? "Restart Nebula to finish the update."
      : `You’re using ${availableUpdate.currentVersion}.`;
  const ActionIcon = installing ? RefreshCw : restartReady ? RotateCcw : Download;
  const actionLabel = installing
    ? "Installing…"
    : restartReady
      ? "Restart now"
      : failed
        ? "Retry update"
        : "Update now";

  return (
    <section className={`workspace-state-banner update${failed ? " failed" : ""}`} role={failed ? "alert" : "status"}>
      <span>
        <strong>{title}</strong>
        {detail && <small>{detail}</small>}
      </span>
      <div className="update-banner-actions">
        <button
          className="button primary"
          type="button"
          disabled={installing}
          onClick={() => void (restartReady ? restart() : install())}
        >
          <ActionIcon className={installing ? "spin" : undefined} size={14} aria-hidden="true" /> {actionLabel}
        </button>
        <button className="icon-button subtle" type="button" aria-label="Dismiss update notification" onClick={dismiss}>
          <X size={15} aria-hidden="true" />
        </button>
      </div>
    </section>
  );
}
