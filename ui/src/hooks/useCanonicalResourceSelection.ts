import { useEffect, useRef } from "react";
import { useNavigate, useParams } from "react-router-dom";
import type { ResourceKind } from "../api/types";
import { resourcePath } from "../resourceRoutes";
import { useWorkspace } from "../state/WorkspaceContext";

export function useCanonicalResourceSelection<T extends { id: string }>(
  kind: ResourceKind,
  items: T[],
  selected: T | undefined,
  setSelected: (value: T | undefined) => void,
) {
  const navigate = useNavigate();
  const { resourceId } = useParams();
  const { engagement } = useWorkspace();
  const requested = resourceId ? items.find((item) => item.id === resourceId) : undefined;
  const previousResourceId = useRef(resourceId);

  useEffect(() => {
    const changedInHistory = previousResourceId.current !== resourceId;
    previousResourceId.current = resourceId;
    if (!resourceId) {
      return;
    }
    if (requested && (!selected || changedInHistory) && selected?.id !== requested.id) setSelected(requested);
  }, [requested, resourceId, selected, setSelected]);

  return {
    missingResourceId: resourceId && !requested ? resourceId : undefined,
    openResource: (item: T) => navigate(resourcePath(engagement?.id, kind, item.id)),
    closeResource: () => navigate(resourcePath(engagement?.id, kind)),
  };
}
