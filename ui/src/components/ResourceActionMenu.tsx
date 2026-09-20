import { useEffect, useRef, useState } from "react";
import { ChevronRight, MoreHorizontal } from "lucide-react";
import type { ActionDescriptor, ResourceRef } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { resourcePath } from "../resourceRoutes";
import { useWorkspace } from "../state/WorkspaceContext";

const actionLabels: Record<string, string> = {
  open: "Open",
  ask_nebula: "Ask Nebula",
  take_note: "Take Note",
  preserve_as_evidence: "Preserve as Evidence",
  draft_finding: "Draft Finding",
  add_to_report: "Add to Report",
  open_source: "Open Source",
  download: "Download",
  copy: "Copy",
  reveal: "Reveal",
  navigate: "Navigate",
  configure: "Configure",
};

export type ResourceActionAdapters = Partial<Record<string, () => void | Promise<void>>>;
const emptyAdapters: ResourceActionAdapters = {};

export function ResourceActionMenu({
  resource,
  adapters = emptyAdapters,
}: {
  resource: ResourceRef;
  adapters?: ResourceActionAdapters;
}) {
  const { api } = useWorkspace();
  const [actions, setActions] = useState<ActionDescriptor[]>([]);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  // Callers pass inline adapter objects, so the effect keys on the adapter
  // names it filters by and reads the callbacks through a ref; depending on
  // the object re-issued the actions request on every parent render.
  const adaptersRef = useRef(adapters);
  adaptersRef.current = adapters;
  const adapterKeys = Object.keys(adapters).sort().join("\n");
  useEffect(() => {
    if (!api) return;
    const controller = new AbortController();
    api.resolveResourceActions([resource], undefined, [], controller.signal)
      .then((items) => setActions(items.filter((item) => item.id === "open" || adaptersRef.current[item.id])))
      .catch((error) => {
        if (controller.signal.aborted) return;
        void logCaughtDiagnostic(
          "interface.resource_actions.resolve_failed",
          "Shared resource actions could not be resolved.",
          error,
          "resource_actions",
        );
      });
    return () => controller.abort();
  }, [adapterKeys, api, resource.id, resource.kind, resource.projectId, resource.revision]);

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  const invoke = async (action: ActionDescriptor) => {
    if (!action.available) return;
    if (action.id === "open") {
      window.history.pushState({}, "", resourcePath(resource.projectId, resource.kind, resource.id));
      window.dispatchEvent(new PopStateEvent("popstate"));
    } else {
      await adapters[action.id]?.();
    }
    setOpen(false);
  };
  if (!actions.length) return null;
  return (
    <div ref={rootRef} className="resource-action-menu">
      <button ref={triggerRef} className="button quiet" type="button" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        <MoreHorizontal size={15} /> Actions
      </button>
      {open && (
        <div role="menu" aria-label="Resource actions">
          {actions.map((action) => (
            <button
              key={action.id}
              type="button"
              role="menuitem"
              disabled={!action.available}
              title={action.disabledReason}
              onClick={() => void invoke(action)}
            >
              <span>{actionLabels[action.id] ?? action.id}</span><ChevronRight size={13} />
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
