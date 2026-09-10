declare const __NEBULA_UI_BUILD__: {commit: string; builtAt: string; dirty: boolean};
export const uiBuild = typeof __NEBULA_UI_BUILD__ === "undefined"
  ? {commit: "unknown", builtAt: "unknown", dirty: false} : __NEBULA_UI_BUILD__;

export function buildAlignment(commits: (string | undefined)[], dirty = false): "matching" | "mismatch" | "unverified" {
  const known = commits.filter((value): value is string => Boolean(value && /^[a-f0-9]{7,40}$/i.test(value)));
  if (dirty) return "unverified";
  if (known.length > 1 && known.some(value => !value.startsWith(known[0]) && !known[0].startsWith(value))) return "mismatch";
  return known.length === commits.length && known.length > 1 ? "matching" : "unverified";
}
