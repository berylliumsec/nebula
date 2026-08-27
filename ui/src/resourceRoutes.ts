import type { ResourceKind } from "./api/types";
import { logCaughtDiagnostic } from "./diagnostics";

export type ProjectSurface = "workbench" | "assets" | "evidence" | "sources" | "findings" | "reports";

export const projectRoot = (projectId: string) => `/projects/${encodeURIComponent(projectId)}`;

export function projectSurface(projectId: string, surface: ProjectSurface, resourceId?: string): string {
  const base = `${projectRoot(projectId)}/${surface}`;
  return resourceId ? `${base}/${encodeURIComponent(resourceId)}` : base;
}

export function resourcePath(projectId: string | undefined, kind: ResourceKind, id?: string): string {
  if (kind === "library_item") return id ? `/library/${encodeURIComponent(id)}` : "/library";
  if (!projectId) return "/";
  if (kind === "project") return projectRoot(projectId);
  const surface: Partial<Record<ResourceKind, ProjectSurface>> = {
    asset: "assets", evidence: "evidence", source: "sources", finding: "findings",
    report: "reports", conversation: "workbench",
  };
  const target = surface[kind] ?? "workbench";
  if (kind === "conversation" && id) return `${projectSurface(projectId, target)}?session=${encodeURIComponent(id)}`;
  return projectSurface(projectId, target, id);
}

export function canonicalNavigationPath(legacyPath: string, projectId?: string): string {
  if (legacyPath === "/settings" || legacyPath === "/library") return legacyPath;
  if (!projectId) return legacyPath;
  if (legacyPath === "/") return projectSurface(projectId, "workbench");
  if (legacyPath === "/findings") return projectSurface(projectId, "findings");
  if (legacyPath === "/reports") return projectSurface(projectId, "reports");
  if (legacyPath === "/project") return projectRoot(projectId);
  return legacyPath;
}

export function projectIdFromPath(pathname: string): string | undefined {
  const match = /^\/projects\/([^/]+)(?:\/|$)/.exec(pathname);
  if (!match) return undefined;
  try {
    return decodeURIComponent(match[1]);
  } catch (error) {
    void logCaughtDiagnostic("interface.resource_route.invalid_project_id", "A canonical project route contained invalid URL encoding.", error, "resource-routing");
    return match[1];
  }
}

export function replaceProjectInPath(pathname: string, projectId: string): string {
  return projectIdFromPath(pathname)
    ? pathname.replace(/^\/projects\/[^/]+/, projectRoot(projectId))
    : projectRoot(projectId);
}
