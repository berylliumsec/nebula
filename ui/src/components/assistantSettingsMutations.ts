import type { ApiClient } from "../api/client";

const SCOPE_FIELDS = [
  "allowed_cidrs", "allowed_domains", "allowed_urls", "allowed_ports",
  "allow_all_targets", "bypass_permissions", "not_before", "not_after",
  "prohibited_actions", "local_only", "tool_suggestions", "on_demand_tools",
  "web_search", "web_search_discloses_scope", "always_loaded_tools",
  "max_concurrency", "grants",
] as const;

type ScopeWire = Record<string, unknown> & { revision: number };

/** Save only requested fields using the same revisioned Core route as Project Policy. */
export async function saveAssistantProjectScope(
  api: Pick<ApiClient, "request">,
  projectId: string,
  changes: Record<string, unknown>,
): Promise<ScopeWire> {
  const path = `engagements/${encodeURIComponent(projectId)}/scope`;
  const current = await api.request<ScopeWire>(path);
  if (!Number.isInteger(current.revision) || current.revision < 1) {
    throw new Error("Project scope has no usable revision. Refresh and retry.");
  }
  const body: Record<string, unknown> = Object.fromEntries(
    SCOPE_FIELDS.filter((name) => name in current).map((name) => [name, current[name]]),
  );
  const { mode, ...patch } = changes;
  Object.assign(body, patch);
  if (mode === "bounded") {
    body.allow_all_targets = false;
    body.bypass_permissions = false;
  } else if (mode === "all_destinations") {
    body.allow_all_targets = true;
    body.bypass_permissions = false;
  } else if (mode === "allow_all") {
    body.allow_all_targets = true;
    body.bypass_permissions = true;
    body.local_only = false;
  }
  body.expected_revision = current.revision;
  await api.request<ScopeWire>(path, { method: "PUT", body: JSON.stringify(body) });
  const observed = await api.request<ScopeWire>(path);
  // The first save turns Core's virtual revision-1 default into a stored
  // revision-1 record. Later edits increment the revision.
  if (!Number.isInteger(observed.revision) || observed.revision < current.revision) {
    throw new Error("Core did not confirm the project scope revision. Refresh and retry.");
  }
  if (mode && (observed.allow_all_targets !== body.allow_all_targets || observed.bypass_permissions !== body.bypass_permissions)) {
    throw new Error("Core did not retain the requested authorization mode. Refresh Project Policy.");
  }
  for (const [field, requested] of Object.entries(patch)) {
    const received = observed[field];
    const matches = field === "not_before" || field === "not_after"
      ? (requested === null && received === null) || (typeof requested === "string" && typeof received === "string" && Date.parse(requested) === Date.parse(received))
      : JSON.stringify(received) === JSON.stringify(requested);
    if (!matches) throw new Error(`Core did not retain ${field}. Refresh Project Policy.`);
  }
  return observed;
}
