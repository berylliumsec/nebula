import { useEffect, useState } from "react";
import type { ApiClient } from "../api/client";
import { OPEN_PROJECT, type ProjectHoldState } from "../api/projectHold";

type HoldClient = Pick<ApiClient, "projectHolds"> | null | undefined;

/** The project's Core hold, read by one shared watcher per client. */
export function useProjectHold(api: HoldClient, projectId: string | undefined): ProjectHoldState {
  const watch = api?.projectHolds;
  const [state, setState] = useState<ProjectHoldState>(() => watch?.state(projectId) ?? OPEN_PROJECT);
  useEffect(() => {
    if (!watch || !projectId) { setState(OPEN_PROJECT); return; }
    setState(watch.state(projectId));
    return watch.subscribe(projectId, setState);
  }, [watch, projectId]);
  return state;
}
