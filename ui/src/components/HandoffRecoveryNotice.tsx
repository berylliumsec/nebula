import { useEffect, useState } from "react";
import { ArrowRight, RefreshCw, X } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { desktopDeviceId } from "../api/runtime";
import type { HandoffResolution } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { projectSurface, resourcePath } from "../resourceRoutes";
import { useWorkbenchDrafts } from "../state/WorkbenchDraftContext";
import { useWorkspace } from "../state/WorkspaceContext";

export function HandoffRecoveryNotice() {
  const { api } = useWorkspace();
  const { activeHandoffIds } = useWorkbenchDrafts();
  const location = useLocation();
  const navigate = useNavigate();
  const handoffId = new URLSearchParams(location.search).get("handoff") ?? "";
  const [resolution, setResolution] = useState<HandoffResolution>();
  const [state, setState] = useState<"idle" | "loading" | "failed" | "acting">("idle");
  const [retry, setRetry] = useState(0);

  const dismiss = () => {
    const parameters = new URLSearchParams(location.search);
    parameters.delete("handoff");
    navigate(`${location.pathname}${parameters.size ? `?${parameters}` : ""}`, { replace: true });
  };

  useEffect(() => {
    if (!api || !handoffId || activeHandoffIds.includes(handoffId)) {
      setResolution(undefined);
      setState("idle");
      return;
    }
    const controller = new AbortController();
    setState("loading");
    void desktopDeviceId()
      .then((deviceId) => api.resolveHandoff(handoffId, deviceId, controller.signal))
      .then((value) => {
        setResolution(value);
        setState("idle");
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        void logCaughtDiagnostic("interface.handoff.resolve_failed", "A durable handoff could not be restored.", error, "handoffs");
        setState("failed");
      });
    return () => controller.abort();
  }, [activeHandoffIds, api, handoffId, retry]);

  if (!handoffId || activeHandoffIds.includes(handoffId)) return null;
  if (state === "loading") return <div className="workspace-state-banner starting" role="status"><span><strong>Restoring handoff…</strong><small>Checking source revisions without loading unsent bytes.</small></span></div>;
  if (state === "failed") return <div className="workspace-state-banner degraded" role="alert"><span><strong>Handoff unavailable</strong><small>The current surface still works. Retry the handoff or dismiss this notice.</small></span><button className="button quiet" type="button" onClick={() => setRetry((value) => value + 1)}>Retry</button><button className="icon-button subtle" type="button" aria-label="Dismiss handoff notice" onClick={dismiss}><X size={15} /></button></div>;
  if (!resolution) return null;

  const { envelope } = resolution;
  if (envelope.status !== "pending") return <div className="workspace-state-banner degraded" role="status"><span><strong>Handoff {envelope.status}</strong><small>This handoff can no longer be consumed. The current surface remains available.</small></span><button className="button quiet" type="button" onClick={dismiss}>Dismiss</button></div>;

  const openSource = () => {
    const source = envelope.targetRef ?? envelope.sourceRefs[0];
    if (source) navigate(resourcePath(source.projectId, source.kind, source.id));
  };
  const consume = async () => {
    if (!api) return;
    setState("acting");
    try {
      const deviceId = await desktopDeviceId();
      await api.consumeHandoff(envelope.id, envelope.revision, deviceId, `consume:${deviceId}:${envelope.id}`);
      if (envelope.targetRef) {
        navigate(resourcePath(envelope.targetRef.projectId, envelope.targetRef.kind, envelope.targetRef.id));
        return;
      }
      const actionDestinations: Record<string, string> = {
        ask_nebula: `${projectSurface(envelope.projectId, "workbench")}?view=chat`,
        take_note: `${projectSurface(envelope.projectId, "workbench")}?view=notes`,
        run: `${projectSurface(envelope.projectId, "workbench")}?view=terminal`,
        draft_finding: `/projects/${encodeURIComponent(envelope.projectId)}/findings`,
        add_to_report: `/projects/${encodeURIComponent(envelope.projectId)}/reports`,
      };
      navigate(actionDestinations[envelope.actionId] ?? projectSurface(envelope.projectId, "workbench"), { replace: true });
    } catch (error) {
      void logCaughtDiagnostic("interface.handoff.consume_failed", "A durable handoff could not be consumed.", error, "handoffs");
      setState("failed");
    }
  };

  const changed = resolution.sources.some((source) => source.state === "changed" || source.state === "deleted");
  const title = resolution.recovery === "resume_origin"
    ? "Resume on the originating device"
    : resolution.recovery === "preserve_or_recapture"
      ? "Recapture transient context"
      : changed ? "Source changed" : "Handoff ready";
  const detail = resolution.recovery === "resume_origin"
    ? `Unsent selected bytes remained only in memory on ${envelope.originDeviceId}. Return to that still-open Nebula window, or open the durable source here and recapture.`
    : resolution.recovery === "preserve_or_recapture"
      ? "The source reference and hash survived, but unsent bytes were intentionally not stored. Select the text again or preserve the source before continuing."
      : "The durable source still matches its recorded revision and hash.";
  return <div className="workspace-state-banner degraded handoff-recovery" role="alert">
    <span><strong>{title}</strong><small>{detail}</small></span>
    {envelope.sourceRefs.length > 0 && <button className="button quiet" type="button" onClick={openSource}><RefreshCw size={14} /> Open source</button>}
    {resolution.recovery === "ready" && <button className="button primary" type="button" disabled={state === "acting"} onClick={() => void consume()}>Continue <ArrowRight size={14} /></button>}
    <button className="icon-button subtle" type="button" aria-label="Dismiss handoff notice" onClick={dismiss}><X size={15} /></button>
  </div>;
}
