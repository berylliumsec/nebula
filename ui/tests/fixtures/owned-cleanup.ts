/** Teardown only resources explicitly owned by this fixture, even after a failure. */
export interface OwnedCleanup {
  name: string;
  dispose(): void | Promise<void>;
}

export class OwnedCleanupError extends AggregateError {
  constructor(readonly failures: Array<{name: string; error: unknown}>) {
    super(failures.map(({name, error}) => new Error(`Cleanup failed: ${name}`, {cause: error})),
      `Owned resource cleanup failed: ${failures.map(({name}) => name).join(", ")}`);
    this.name = "OwnedCleanupError";
  }
}

export async function withOwnedCleanup<T>(
  body: () => Promise<T>,
  resources: OwnedCleanup[],
  report: (error: OwnedCleanupError) => void = error => console.error(error),
): Promise<T> {
  let value: T | undefined;
  let failed = false;
  let primary: unknown;
  try { value = await body(); } catch (error) { failed = true; primary = error; }
  const failures: Array<{name: string; error: unknown}> = [];
  for (const resource of resources) {
    try { await resource.dispose(); } catch (error) { failures.push({name: resource.name, error}); }
  }
  if (failures.length) {
    const secondary = new OwnedCleanupError(failures);
    if (!failed) throw secondary;
    // Reporting must never replace the original test/startup failure, even for frozen errors.
    try { report(secondary); } catch { /* The original error remains authoritative. */ }
  }
  if (failed) throw primary;
  return value as T;
}
