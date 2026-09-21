/** The highest running-at-once subagent limit Core accepts. */
export const SUBAGENT_LIMIT_MAX = 100;

/** A valid limit from stored metadata or typed input; anything else is no limit. */
export function subagentLimit(value: unknown): number | undefined {
  const parsed = typeof value === "string" && value.trim() ? Number(value) : value;
  return typeof parsed === "number" && Number.isInteger(parsed) && parsed >= 1 && parsed <= SUBAGENT_LIMIT_MAX
    ? parsed
    : undefined;
}
