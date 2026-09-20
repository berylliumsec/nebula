/**
 * localStorage access that tolerates blocked site data (private windows,
 * "block all cookies", embedded shells): the getter itself can throw there,
 * and a remembered preference is never worth failing the first render.
 */
export function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null; // diagnostic-expected: storage unavailable; the default applies for this launch
  }
}

export function writeStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // diagnostic-expected: the choice still applies for this launch when storage is unavailable.
  }
}

export function removeStorage(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    // diagnostic-expected: nothing was remembered when storage is unavailable.
  }
}
