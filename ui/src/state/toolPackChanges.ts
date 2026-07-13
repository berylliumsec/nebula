import { useSyncExternalStore } from "react";

let revision = 0;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function snapshot(): number {
  return revision;
}

export function notifyToolPacksChanged(): void {
  revision += 1;
  for (const listener of listeners) listener();
}

export function useToolPackRevision(): number {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}
