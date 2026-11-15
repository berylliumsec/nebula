import type { ToolOutputReadResult, ToolOutputSearchResult } from "../api/types";

/**
 * Add the next page of artifact search matches to the ones already shown.
 * Core's cursor and truncation describe the newest page; skipped artifacts are
 * reported once.
 */
export function appendSearchPage(previous: ToolOutputSearchResult | undefined, next: ToolOutputSearchResult): ToolOutputSearchResult {
  if (!previous) return next;
  const skipped = [...previous.skipped];
  for (const item of next.skipped) if (!skipped.some((seen) => seen.artifactId === item.artifactId)) skipped.push(item);
  return { ...next, matches: [...previous.matches, ...next.matches], skipped };
}

export interface ReadContinuation {
  label: string;
  startingLine: number;
  cursor?: string;
}

/**
 * How to read the page after this one. Core resumes with its cursor, which is
 * the only way past a page that stopped inside one long line; the starting
 * line is a fallback for a Core that sends none.
 */
export function readContinuation(read: ToolOutputReadResult): ReadContinuation | undefined {
  if (!read.truncated && !read.continuationCursor && !read.continuationStartingLine) return undefined;
  if (read.continuationStartingLine) {
    return { label: "Read next lines", startingLine: read.continuationStartingLine, cursor: read.continuationCursor };
  }
  if (!read.continuationCursor) return undefined;
  const line = read.lines.at(-1)?.line;
  return { label: line ? `Continue line ${line}` : "Read more", startingLine: line ?? 1, cursor: read.continuationCursor };
}
