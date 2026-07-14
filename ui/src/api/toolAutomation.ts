import type { ApiClient } from "./client";
import type { ToolSummary } from "./types";

export interface AutomaticToolSelection {
  tools: ToolSummary[];
  unavailableReason?: string;
}

/** Resolve all available project tools, provisioning the signed official pack
 * for a new project when no usable assignment exists yet. */
export async function selectProjectTools(
  api: ApiClient,
  engagementId: string,
): Promise<AutomaticToolSelection> {
  const [assignments, installedTools, installedPacks] = await Promise.all([
    api.listEngagementToolAssignments(engagementId),
    api.listTools(),
    api.listToolPacks(),
  ]);
  const readyDigests = new Set(installedPacks
    .filter((pack) => pack.status === "ready")
    .map((pack) => pack.manifestDigest));
  const readyAssignments = assignments.filter((assignment) => assignment.enabled
    && assignment.manifestDigest !== undefined
    && readyDigests.has(assignment.manifestDigest));
  const selected = installedTools.filter((tool) => tool.available
    && readyAssignments.some((assignment) => assignment.manifestDigest === tool.packManifestDigest
      && assignment.toolNames.includes(tool.name)));
  if (selected.length) return { tools: selected };

  const [catalog, runners] = await Promise.all([api.listToolCatalog(), api.listRunnerProfiles()]);
  const officialEntries = catalog.filter((entry) => entry.signed
    && entry.publisher === "berylliumsec"
    && (entry.collectionId === "nebula-toolbox" || entry.name === "nebula-toolbox"));
  const runner = runners.find((candidate) => candidate.state === "ready");
  if (!runner) return { tools: [], unavailableReason: "A verified local runtime is required before tools can be used." };
  if (!officialEntries.length) return { tools: [], unavailableReason: "The signed Nebula Toolbox is not available in the configured catalog." };

  const officialDigests = new Set(officialEntries.map((entry) => entry.manifestDigest));
  let readyOfficialPacks = installedPacks.filter((pack) => pack.status === "ready"
    && pack.trustState === "trusted"
    && officialDigests.has(pack.manifestDigest));
  if (!readyOfficialPacks.length) {
    const collectionId = officialEntries.find((entry) => entry.collectionId)?.collectionId;
    const installed = collectionId
      ? await api.installToolCollection(collectionId, runner.id)
      : [await api.installToolPack(officialEntries[0].id, runner.id, officialEntries[0].version)];
    readyOfficialPacks = installed.filter((pack) => pack.status === "ready"
      && pack.trustState === "trusted"
      && officialDigests.has(pack.manifestDigest));
  }
  if (!readyOfficialPacks.length) {
    return { tools: [], unavailableReason: "The official Toolbox did not reach a verified ready state." };
  }

  const latestTools = await api.listTools();
  const savedAssignments = await Promise.all(readyOfficialPacks.map((pack) => api.updateEngagementToolAssignment(
    engagementId,
    {
      manifestDigest: pack.manifestDigest,
      toolNames: latestTools
        .filter((tool) => tool.available && tool.packManifestDigest === pack.manifestDigest)
        .map((tool) => tool.name),
      enabled: true,
    },
  )));
  const prepared = latestTools.filter((tool) => tool.available
    && savedAssignments.some((assignment) => assignment.enabled
      && assignment.manifestDigest === tool.packManifestDigest
      && assignment.toolNames.includes(tool.name)));
  return {
    tools: prepared,
    unavailableReason: prepared.length ? undefined : "The official Toolbox exposes no available capabilities.",
  };
}
