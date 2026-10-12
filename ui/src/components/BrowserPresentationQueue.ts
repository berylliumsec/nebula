import type { BrowserBounds } from "../api/workbenchBrowser";

export interface BrowserPresentation {
  projectId: string;
  tabId: string | null;
  bounds: BrowserBounds | null;
}

interface PresentationCallbacks {
  onSuccess?: () => void;
  onError?: (error: unknown) => void;
}

// Native view commands are asynchronous. Keep their order while replacing
// unstarted resize requests with the most recent measured slot.
export class BrowserPresentationQueue {
  private pending: Array<{ presentation: BrowserPresentation; callbacks: PresentationCallbacks }> = [];
  private running = false;
  private idleWaiters: Array<() => void> = [];

  constructor(private readonly apply: (presentation: BrowserPresentation) => Promise<void>) {}

  request(presentation: BrowserPresentation, callbacks: PresentationCallbacks = {}): void {
    const last = this.pending.at(-1);
    // A hide must complete before a later show. Other unstarted placements can
    // be replaced by the latest request, including a hide replacing a show.
    if (last && (last.presentation.tabId !== null || presentation.tabId === null)) {
      this.pending[this.pending.length - 1] = { presentation, callbacks };
    } else {
      this.pending.push({ presentation, callbacks });
    }
    if (!this.running) void this.drain();
  }

  whenIdle(): Promise<void> {
    if (!this.running && this.pending.length === 0) return Promise.resolve();
    return new Promise((resolve) => this.idleWaiters.push(resolve));
  }

  private async drain(): Promise<void> {
    this.running = true;
    while (this.pending.length > 0) {
      const { presentation, callbacks } = this.pending.shift()!;
      let succeeded = false;
      let failure: unknown;
      try {
        await this.apply(presentation);
        succeeded = true;
      } catch (error) {
        failure = error;
      }
      try {
        if (succeeded) callbacks.onSuccess?.();
        else callbacks.onError?.(failure);
      } catch (error) {
        console.error("Browser presentation callback failed", error);
      }
    }
    this.running = false;
    for (const resolve of this.idleWaiters.splice(0)) resolve();
  }
}
