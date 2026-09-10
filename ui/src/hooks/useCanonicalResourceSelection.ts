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
  const opener = useRef<HTMLElement | null>(null);
  const focusFrame = useRef<number | undefined>(undefined);
  useEffect(() => () => { if (focusFrame.current !== undefined) cancelAnimationFrame(focusFrame.current); }, []);

  useEffect(() => {
    const changedInHistory = previousResourceId.current !== resourceId;
    previousResourceId.current = resourceId;
    // Navigation owns which record is presented. Keeping a previous selection
    // after Close/Back leaves an invisible-route inspector blocking the page.
    // Same-ID refreshes deliberately retain the edit snapshot for revision
    // conflict checks; navigation is not an implicit refresh of that snapshot.
    if (changedInHistory && selected?.id !== resourceId) {
      setSelected(requested);
      return;
    }
    if (!resourceId) {
      return;
    }
    if (requested && (!selected || changedInHistory) && selected?.id !== requested.id) setSelected(requested);
  }, [requested, resourceId, selected, setSelected]);

  return {
    missingResourceId: resourceId && !requested ? resourceId : undefined,
    openResource: (item: T) => {
      if (focusFrame.current !== undefined) cancelAnimationFrame(focusFrame.current);
      opener.current = document.activeElement instanceof HTMLElement && document.activeElement !== document.body ? document.activeElement : null;
      navigate(resourcePath(engagement?.id, kind, item.id));
    },
    closeResource: () => {
      setSelected(undefined);
      navigate(resourcePath(engagement?.id, kind));
      if (focusFrame.current !== undefined) cancelAnimationFrame(focusFrame.current);
      focusFrame.current = requestAnimationFrame(() => {
        const target = opener.current?.isConnected ? opener.current : document.querySelector<HTMLElement>('.page input[type="search"]');
        target?.focus({preventScroll: true});
        focusFrame.current = undefined;
      });
    },
  };
}
