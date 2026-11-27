/** Butler mode, the same page desktop shows, opened from one project inside the iPhone app. */
export function shellButlerPath(projectId: string): string {
  return `/butler?project=${encodeURIComponent(projectId)}`;
}
