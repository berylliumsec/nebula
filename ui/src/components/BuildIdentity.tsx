import { useEffect, useState } from "react";
import { getReleaseInfo } from "../api/updater";
import { buildAlignment, uiBuild } from "../buildIdentity";
import { logCaughtDiagnostic } from "../diagnostics/logger";

export function BuildIdentity({coreCommit}: {coreCommit?: string}) {
  const native = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
  const [desktopCommit, setDesktopCommit] = useState<string>();
  useEffect(() => {
    if (!native) return;
    let active = true;
    void getReleaseInfo().then(value => {if (active) setDesktopCommit(value.commit);}).catch(error => {
      void logCaughtDiagnostic("interface.build_identity.unavailable", "Desktop build identity could not be read.", error, "diagnostics");
    });
    return () => {active = false;};
  }, [native]);
  const alignment = buildAlignment([uiBuild.commit, coreCommit, ...(native ? [desktopCommit] : [])], uiBuild.dirty);
  return <section className="build-identity" aria-label="Installed build identity">
    <strong>{alignment === "matching" ? "Builds match" : alignment === "mismatch" ? "Build mismatch" : "Build alignment unverified"}</strong>
    <p>Interface <code title={uiBuild.commit}>{uiBuild.commit.slice(0, 12)}{uiBuild.dirty ? " · local changes" : ""}</code> · Core <code title={coreCommit}>{coreCommit?.slice(0, 12) ?? "unknown"}</code>{native && <> · Desktop <code title={desktopCommit}>{desktopCommit?.slice(0, 12) ?? "unknown"}</code></>}</p>
    {alignment === "mismatch" && <p role="status">The interface and running application are different builds. Update the app and server together; reloading alone may not update the desktop bundle.</p>}
  </section>;
}
