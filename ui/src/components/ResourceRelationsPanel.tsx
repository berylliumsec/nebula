import { useEffect, useMemo, useState } from "react";
import { GitBranch, Link2 } from "lucide-react";
import type { ResourceRef, ResourceRelation } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { useWorkspace } from "../state/WorkspaceContext";

const inverseLabels: Record<ResourceRelation["predicate"], string> = {
  affects: "Affected by",
  supports: "Supported by",
  includes: "Included in",
  references: "Referenced by",
  produced_by: "Produced",
  derived_from: "Source of",
};

const forwardLabels: Record<ResourceRelation["predicate"], string> = {
  affects: "Affects",
  supports: "Supports",
  includes: "Includes",
  references: "References",
  produced_by: "Produced by",
  derived_from: "Derived from",
};

export function ResourceRelationsPanel({ resource }: { resource: ResourceRef }) {
  const { api } = useWorkspace();
  const [relations, setRelations] = useState<ResourceRelation[]>([]);
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");

  useEffect(() => {
    if (!api || !resource.projectId) return;
    const controller = new AbortController();
    setState("loading");
    api.listResourceRelations(resource.projectId, resource, undefined, controller.signal)
      .then(async (items) => {
        const counterparts = items.map((item) => item.source.id === resource.id ? item.target : item.source);
        const resolutions = await Promise.all(counterparts.map((ref) => api.resolveResource(ref, controller.signal)));
        setRelations(items);
        setLabels(Object.fromEntries(resolutions.map((item) => [item.ref.id, item.label])));
        setState("ready");
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        void logCaughtDiagnostic(
          "interface.resource_relations.load_failed",
          "Shared resource connections could not be loaded.",
          error,
          "resource_relations",
        );
        setState("failed");
      });
    return () => controller.abort();
  }, [api, resource.id, resource.kind, resource.projectId, resource.revision]);

  const ordered = useMemo(
    () => [...relations].sort((left, right) => left.createdAt.localeCompare(right.createdAt)),
    [relations],
  );
  return (
    <section className="resource-relations" aria-labelledby="resource-relations-title">
      <h3 id="resource-relations-title"><Link2 size={15} /> Connections</h3>
      {state === "loading" && <p role="status">Loading connections…</p>}
      {state === "failed" && <p role="alert">Connections are temporarily unavailable. The resource is still usable.</p>}
      {state === "ready" && !ordered.length && <p>No connected resources yet.</p>}
      {state === "ready" && ordered.length > 0 && (
        <ol className="resource-lineage" aria-label="Resource lineage">
          {ordered.map((item) => {
            const outgoing = item.source.id === resource.id && item.source.kind === resource.kind;
            const counterpart = outgoing ? item.target : item.source;
            return (
              <li key={item.id}>
                <GitBranch size={14} aria-hidden="true" />
                <span><small>{outgoing ? forwardLabels[item.predicate] : inverseLabels[item.predicate]}</small><strong>{labels[counterpart.id] ?? `${counterpart.kind.replaceAll("_", " ")} (unavailable)`}</strong></span>
                <time dateTime={item.createdAt}>{new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(item.createdAt))}</time>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
