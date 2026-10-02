export interface WorkspaceLink { path: string; line?: number }

/** Accept only files in the selected project's workspace. Core checks the file again on read. */
export function workspaceLink(href: string, workspacePath?: string): WorkspaceLink | undefined {
  if (!href || href.startsWith("//") || href.includes("?") || /[\\\u0000-\u001f]/.test(href)) return undefined;
  let value: string;
  try { value = decodeURIComponent(href); } catch { /* diagnostic-expected: malformed agent links stay inert. */ return undefined; }
  if (value.includes("%") || value.startsWith("//") || /[\\\u0000-\u001f]/.test(value)) return undefined;
  const fragment = value.match(/#L(\d+)(?:C\d+)?$/i);
  if (fragment) value = value.slice(0, fragment.index);
  const suffix = value.match(/:(\d+)$/);
  if (suffix) value = value.slice(0, suffix.index);
  if (value.includes("#") || /^[a-z][a-z\d+.-]*:/i.test(value)) return undefined;
  const root = workspacePath?.replace(/\/+$/, "");
  if (value.startsWith("/workspace/")) value = value.slice("/workspace/".length);
  else if (value.startsWith("/")) {
    if (!root || !value.startsWith(`${root}/`)) return undefined;
    value = value.slice(root.length + 1);
  }
  const segments = value.split("/").filter(segment => segment !== ".");
  if (!segments.length || segments.some(segment => !segment || segment === "..")) return undefined;
  const line = fragment?.[1] ?? suffix?.[1];
  if (line && (!Number.isSafeInteger(Number(line)) || Number(line) < 1)) return undefined;
  return { path: segments.join("/"), ...(line ? { line: Number(line) } : {}) };
}

export function webLink(href: string): boolean {
  return URL.canParse(href) && ["http:", "https:"].includes(new URL(href).protocol);
}
