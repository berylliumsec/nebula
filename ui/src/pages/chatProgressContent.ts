import { parseExactFences } from "../components/assistantCode";

/** Core keeps routing prose ahead of the final answer in one durable message. */
export function splitSavedProgress(
  content: string,
  metadata?: Record<string, unknown>,
): { answer: string; progress?: string } {
  const boundary = metadata?.progress_prefix_utf16_length;
  if (typeof boundary !== "number" || !Number.isSafeInteger(boundary) || boundary < 1
      || content.slice(boundary, boundary + 2) !== "\n\n"
      || boundary + 2 >= content.length) {
    return { answer: content };
  }
  // A fence that spans the boundary cannot be rendered as two independent
  // Markdown pieces without changing its meaning or executable source.
  if (parseExactFences(content.slice(0, boundary)).unmatchedStart !== undefined) {
    return { answer: content };
  }
  return {
    answer: content.slice(boundary + 2),
    progress: content.slice(0, boundary),
  };
}

/** A small literal preview; the exact text stays available in View work. */
export function latestProgressPreview(content: string): string {
  const blocks = content.trim().split(/\r?\n\s*\r?\n/).filter(Boolean);
  const latest = (blocks.at(-1) ?? "").replace(/\s+/g, " ").trim();
  const characters = Array.from(latest);
  return characters.length > 240 ? `${characters.slice(0, 239).join("")}…` : latest;
}
