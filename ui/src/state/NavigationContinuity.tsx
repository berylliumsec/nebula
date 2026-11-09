import { useEffect, useRef, useState, type PropsWithChildren } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { projectSurface } from "../resourceRoutes";
import { useWorkspace } from "./WorkspaceContext";
import { isApplicationEntry, rememberChat, rememberedChatPath, rememberedNavigation } from "./navigationContinuity";

/** Restore only this document's plain launch route, after authenticated bootstrap. */
export function NavigationContinuity({children}: PropsWithChildren) {
  const location = useLocation();
  const { continuityScope, engagement, engagements, resourceStatus, workspaceState } = useWorkspace();
  const entry = useRef(isApplicationEntry(location));
  const [limited, setLimited] = useState(false);
  const ready = workspaceState === "ready" || workspaceState === "degraded";
  const atEntry = isApplicationEntry(location);

  useEffect(() => { if (!atEntry) entry.current = false; }, [atEntry]);
  useEffect(() => {
    if (!ready || !engagement || location.pathname !== projectSurface(engagement.id, "workbench")) return;
    const params = new URLSearchParams(location.search);
    if (params.get("view") !== "chat") return;
    setLimited(!rememberChat(continuityScope, engagement.id, params.get("session") || undefined));
  }, [continuityScope, engagement?.id, location.pathname, location.search, ready]);

  if (entry.current && atEntry) {
    if (workspaceState === "starting" || workspaceState === "bootstrapping") return <div className="route-loading" role="status">Opening workspace…</div>;
    if (ready && continuityScope && resourceStatus.projects.state !== "failed") {
      const saved = rememberedNavigation(continuityScope);
      const target = rememberedChatPath(continuityScope);
      if (target && engagements.some(project => project.id === saved?.projectId)) return <Navigate to={target} replace />;
      entry.current = false;
    }
  }
  return <>{limited && <p className="connection-notice" role="status">This device could not remember the selected chat. Keep its link to reopen it.</p>}{children}</>;
}
