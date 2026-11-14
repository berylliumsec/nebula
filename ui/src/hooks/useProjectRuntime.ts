import { useEffect, useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import type { ProjectRuntimeState } from "../api/projectRuntime";

type RuntimeClient = Pick<ApiClient, "projectRuntime"> | null | undefined;

/**
 * While `waitKey` is nonzero, call `onRecovered` once the project runtime
 * answers again. Callers bump `waitKey` each time a read finds the runtime
 * away and reset it when they read again, so a failed retry waits again.
 */
export function useProjectRuntimeRecovery(api: RuntimeClient, waitKey: number, onRecovered: () => void): void {
  const callback = useRef(onRecovered);
  callback.current = onRecovered;
  useEffect(() => {
    const recovery = api?.projectRuntime;
    if (!recovery || !waitKey) return;
    const controller = new AbortController();
    recovery.waitForRuntime(controller.signal).then(
      () => { if (!controller.signal.aborted) callback.current(); },
      () => { /* diagnostic-expected: the view closed or read again before the runtime returned. */ },
    );
    return () => controller.abort();
  }, [api, waitKey]);
}

/** "reconnecting" while any view waits for its project's worker. */
export function useProjectRuntimeState(api: RuntimeClient): ProjectRuntimeState {
  const recovery = api?.projectRuntime;
  const [state, setState] = useState<ProjectRuntimeState>(() => recovery?.state ?? "available");
  useEffect(() => {
    if (!recovery) { setState("available"); return; }
    setState(recovery.state);
    return recovery.subscribe(setState);
  }, [recovery]);
  return state;
}
