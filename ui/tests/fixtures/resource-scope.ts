import {withOwnedCleanup, type OwnedCleanup} from "./owned-cleanup";

/** Register each resource immediately after acquisition, before the next can fail. */
export class ResourceScope {
  private resources: OwnedCleanup[] = [];
  private evidence: Array<{name: string; read: () => string | Promise<string>}> = [];
  private closing?: Promise<void>;

  own<T>(name: string, resource: T, dispose: (value: T) => void | Promise<void>, diagnostics?: (value: T) => string | Promise<string>): T {
    if (this.closing) throw new Error("Cannot register a resource after teardown starts");
    this.resources.push({name, dispose: () => dispose(resource)});
    if (diagnostics) this.evidence.push({name, read: () => diagnostics(resource)});
    return resource;
  }

  async diagnostics(): Promise<string> {
    const sections: string[] = [];
    for (const evidence of this.evidence) {
      try { sections.push(`${evidence.name}\n${(await evidence.read()).slice(-64_000)}`); }
      catch { sections.push(`${evidence.name}\nDiagnostics unavailable.`); }
    }
    return sections.join("\n\n");
  }

  close(): Promise<void> {
    // LIFO keeps dependencies alive until their users have closed. A failed
    // disposer cannot prevent the remaining resources from being attempted.
    this.closing ??= withOwnedCleanup(async () => undefined, [...this.resources].reverse());
    return this.closing;
  }
}
