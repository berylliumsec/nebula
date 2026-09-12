/** Viewer recovery never retries a submission. These waits are abortable on detach. */
export function waitForChatReconnect(attempt: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const finish = () => { cleanup(); resolve(); };
    const abort = () => { cleanup(); reject(new DOMException("Viewer detached", "AbortError")); };
    const visible = () => { if (document.visibilityState === "visible") finish(); };
    const timer = setTimeout(finish, Math.min(10_000, 500 * 2 ** attempt) + Math.random() * 250);
    const cleanup = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      globalThis.removeEventListener("online", finish);
      document.removeEventListener("visibilitychange", visible);
    };
    signal?.addEventListener("abort", abort, {once: true});
    globalThis.addEventListener("online", finish);
    document.addEventListener("visibilitychange", visible);
    if (signal?.aborted) abort();
  });
}

export async function readChatChunk(reader: ReadableStreamDefaultReader<Uint8Array>): Promise<ReadableStreamReadResult<Uint8Array>> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      reader.read(),
      new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error("Chat connection stopped responding")), 45_000); }),
    ]);
  } finally { clearTimeout(timer); }
}
