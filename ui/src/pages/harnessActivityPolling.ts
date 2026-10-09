/** Poll Core's harness state without stacking requests when a read is slow. */
export function startHarnessActivityPolling<T extends {busy: boolean}>(options: {
  read: (signal: AbortSignal) => Promise<T>;
  onValue: (value: T) => void;
  onError: (error: unknown) => void;
  activeTurn: boolean;
  visibility?: Pick<Document, "hidden" | "addEventListener" | "removeEventListener">;
}): () => void {
  const visibility = options.visibility ?? document;
  const controller = new AbortController();
  let stopped = false;
  let reading = false;
  let refreshOnComplete = false;
  let busy = options.activeTurn;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const schedule = (delay: number) => {
    if (timer !== undefined) clearTimeout(timer);
    timer = setTimeout(() => void refresh(), delay);
  };
  const refresh = async () => {
    if (stopped || visibility.hidden) return;
    if (reading) { refreshOnComplete = true; return; }
    reading = true;
    try {
      const value = await options.read(controller.signal);
      if (stopped || visibility.hidden) return;
      busy = value.busy || options.activeTurn;
      options.onValue(value);
    } catch (error) {
      if (!stopped && !controller.signal.aborted && !visibility.hidden) options.onError(error);
    } finally {
      reading = false;
      if (!stopped && !visibility.hidden) {
        schedule(refreshOnComplete ? 0 : busy ? 2_000 : 10_000);
      }
      refreshOnComplete = false;
    }
  };
  const onVisibilityChange = () => {
    if (visibility.hidden) {
      if (timer !== undefined) clearTimeout(timer);
      timer = undefined;
    } else {
      schedule(0);
    }
  };
  visibility.addEventListener("visibilitychange", onVisibilityChange);
  void refresh();
  return () => {
    stopped = true;
    controller.abort();
    if (timer !== undefined) clearTimeout(timer);
    visibility.removeEventListener("visibilitychange", onVisibilityChange);
  };
}
