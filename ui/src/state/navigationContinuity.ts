import { projectSurface } from "../resourceRoutes";

/** An authenticated, nonsecret Core/principal identity; never a credential or URL. */
export interface NavigationScope { key: string; persistent: boolean }
export interface RememberedNavigation { projectId: string; sessionId?: string; updatedAt: number }
interface Record extends RememberedNavigation { scope: string }
export const NAVIGATION_STORAGE_KEY = "nebula.navigation.v1";
export const NAVIGATION_LIMITS = { scopes: 32, bytes: 64 * 1024, ageMs: 30 * 24 * 60 * 60 * 1000 };
const bytes = (value: string) => new TextEncoder().encode(value).byteLength;
const identifier = (value: unknown): value is string => typeof value === "string" && value.length > 0
  && value.length <= 500 && !/[\u0000-\u001f\u007f]/.test(value);

function records(): Record[] {
  try {
    const raw = localStorage.getItem(NAVIGATION_STORAGE_KEY);
    if (!raw || bytes(raw) > NAVIGATION_LIMITS.bytes) return [];
    const values: unknown = JSON.parse(raw);
    if (!Array.isArray(values)) return [];
    return values.filter((value): value is Record => value && typeof value === "object"
      && typeof value.scope === "string" && value.scope.length <= 1400
      && identifier(value.projectId) && (value.sessionId === undefined || identifier(value.sessionId))
      && Number.isSafeInteger(value.updatedAt) && value.updatedAt <= Date.now()
      && value.updatedAt >= Date.now() - NAVIGATION_LIMITS.ageMs)
      .sort((left, right) => right.updatedAt - left.updatedAt).slice(0, NAVIGATION_LIMITS.scopes);
  } catch {
    // diagnostic-expected: device-local preferences may be unavailable; explicit navigation remains usable.
    return [];
  }
}

export function rememberedNavigation(scope?: NavigationScope): RememberedNavigation | undefined {
  if (!scope?.persistent) return undefined;
  const value = records().find(item => item.scope === scope.key);
  return value && { projectId: value.projectId, sessionId: value.sessionId, updatedAt: value.updatedAt };
}

function save(scope: NavigationScope | undefined, value?: RememberedNavigation): boolean {
  if (!scope?.persistent || !scope.key || scope.key.length > 1400) return true;
  try {
    const values = records().filter(item => item.scope !== scope.key);
    if (value) values.unshift({ ...value, scope: scope.key });
    values.splice(NAVIGATION_LIMITS.scopes);
    while (bytes(JSON.stringify(values)) > NAVIGATION_LIMITS.bytes) values.pop();
    localStorage.setItem(NAVIGATION_STORAGE_KEY, JSON.stringify(values));
    return true;
  } catch {
    // diagnostic-expected: navigation remains usable and the caller reports its storage limitation.
    return false;
  }
}

export function rememberProject(scope: NavigationScope | undefined, projectId: string): boolean {
  if (!identifier(projectId)) return true;
  const previous = rememberedNavigation(scope);
  return save(scope, { projectId, sessionId: previous?.projectId === projectId ? previous.sessionId : undefined, updatedAt: Date.now() });
}

export function rememberChat(scope: NavigationScope | undefined, projectId: string, sessionId?: string): boolean {
  if (!identifier(projectId) || (sessionId !== undefined && !identifier(sessionId))) return true;
  return save(scope, { projectId, sessionId, updatedAt: Date.now() });
}

export function forgetNavigation(scope?: NavigationScope): void { save(scope); }

export function rememberedChatPath(scope?: NavigationScope): string | undefined {
  const value = rememberedNavigation(scope);
  if (!value?.sessionId) return undefined;
  return `${projectSurface(value.projectId, "workbench")}?${new URLSearchParams({view: "chat", session: value.sessionId})}`;
}

export function isApplicationEntry(location: {pathname: string; search: string; hash: string}): boolean {
  return location.pathname === "/" && !location.search && !location.hash;
}
