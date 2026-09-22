export function pairedCsrfToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const value = document.cookie
    .split("; ")
    .find((item) => item.startsWith("nebula_csrf="))
    ?.slice("nebula_csrf=".length);
  return value ? decodeURIComponent(value) : undefined;
}
