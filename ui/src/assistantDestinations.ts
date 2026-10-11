import destinationData from "../../src/nebula/v3/assistant_destinations.json" with { type: "json" };

export interface AssistantDestination {
  id: string;
  label: string;
  path: string;
  aliases: string[];
}

export const assistantDestinations: AssistantDestination[] = destinationData;

const normalize = (value: string) => value.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

export function assistantDestination(id: string): AssistantDestination | undefined {
  return assistantDestinations.find((entry) => entry.id === id);
}

export function assistantDestinationPath(entry: AssistantDestination, projectId?: string): string | undefined {
  if (!entry.path.includes("{project_id}")) return entry.path;
  return projectId ? entry.path.replace("{project_id}", encodeURIComponent(projectId)) : undefined;
}

/** Resolve only a unique, explicit page name locally. Other wording can use the model's catalog tool. */
export function destinationToOpen(prompt: string): AssistantDestination | undefined {
  const request = prompt.trim().match(/^(?:(?:please|can you|could you|would you)\s+)?(?:open|show(?: me)?|bring up|take me to|go to|find|locate)\s+(.+?)[.!?]*$/i);
  if (!request) return undefined;
  const target = normalize(request[1]).replace(/^(?:the|my|our)\s+/, "").replace(/\s+(?:page|screen|view)$/, "");
  const matches = assistantDestinations.filter((entry) =>
    [entry.label, ...entry.aliases].some((value) => normalize(value).replace(/^(?:the|my|our)\s+/, "") === target));
  return matches.length === 1 ? matches[0] : undefined;
}
