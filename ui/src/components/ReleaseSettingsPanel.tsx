import { Download, PackageCheck, RefreshCw } from "lucide-react";
import { DiagnosticErrorNotice } from "../diagnostics";
import { useReleaseUpdate } from "../state/ReleaseUpdateContext";

export function ReleaseSettingsPanel() {
  const {
    release,
    availableUpdate,
    phase,
    error,
    failure,
    check,
    install,
    restart,
  } = useReleaseUpdate();
  const restartReady = phase === "restart" || (phase === "error" && failure === "restart");

  return (
    <section className="panel release-panel">
      <header className="panel-header compact"><div><h2>About Nebula</h2><p>Build and update channel</p></div><PackageCheck size={19} /></header>
      <dl>
        <div><dt>Desktop version</dt><dd>{release?.version ?? "Detecting…"}</dd></div>
        <div><dt>Distribution</dt><dd>{release?.distribution ?? "Unknown"}</dd></div>
        <div><dt>Build</dt><dd title={release?.commit}>{release?.commit.slice(0, 12) ?? "Unknown"}</dd></div>
        <div><dt>Target</dt><dd>{release?.buildTarget ?? "Unknown"}</dd></div>
        <div><dt>Built</dt><dd>{release?.builtAt ?? "Unknown"}</dd></div>
        {release?.updateChannel && <div><dt>Update channel</dt><dd>{release.updateChannel}</dd></div>}
      </dl>
      <div className="release-actions">
        {release?.updaterEnabled ? (
          restartReady ? (
            <button className="button primary full" type="button" onClick={() => void restart()}>
              <RefreshCw size={15} /> Restart now
            </button>
          ) : availableUpdate ? (
            <button className="button primary full" type="button" disabled={phase === "installing" || phase === "checking"} onClick={() => void install()}>
              <Download className={phase === "installing" ? "spin" : undefined} size={15} /> {phase === "installing" ? "Installing…" : phase === "error" && failure === "install" ? `Retry ${availableUpdate.version}` : `Install ${availableUpdate.version}`}
            </button>
          ) : (
            <button className="button secondary full" type="button" disabled={phase === "checking" || phase === "installing"} onClick={() => void check()}>
              <PackageCheck size={15} /> {phase === "checking" ? "Checking…" : phase === "error" && failure === "check" ? "Check again" : "Check for updates"}
            </button>
          )
        ) : release ? (
          <p>{release.distribution === "managed" ? "Updates are supplied by your package manager." : "Direct updates are unavailable in this build."}</p>
        ) : phase === "loading" ? (
          <p>Detecting update support…</p>
        ) : (
          <p>Update support is unavailable.</p>
        )}
        {phase === "current" && <p role="status">Nebula is up to date.</p>}
        {phase === "restart" && <p role="status">Update installed. Restart Nebula to finish.</p>}
        {phase === "error" && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}
      </div>
    </section>
  );
}
