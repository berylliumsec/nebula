/** The address scanned by a companion must be the reachable origin of this Core. */
export function companionOrigin(input: string, allowInsecureLan: boolean): string | undefined {
  const value = input.trim();
  if (!/^https?:\/\//i.test(value)) return undefined;
  try {
    const url = new URL(value);
    const hostname = url.hostname.toLowerCase();
    const loopback = hostname === "[::1]" || /^127\./.test(hostname)
      || /(^|\.)localhost\.?$/.test(hostname);
    if ((url.protocol !== "https:" && !(allowInsecureLan && url.protocol === "http:"))
      || !hostname || loopback || hostname === "0.0.0.0" || hostname === "[::]"
      || url.username || url.password || url.pathname !== "/" || url.search || url.hash) return undefined;
    return url.origin;
  } catch {
    // diagnostic-expected: invalid operator input cannot produce a pairing link.
    return undefined;
  }
}

export function companionPairingUrl(origin: string, secret: string, code: string): string {
  return `${origin}/#pair=${encodeURIComponent(secret)}&code=${encodeURIComponent(code)}`;
}
