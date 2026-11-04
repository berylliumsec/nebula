import { vi } from "vitest";

/** jsdom has no layout; give the real transcript virtualizer a visible viewport. */
export function mockChatViewport() {
  for (const [property, size] of [["offsetHeight", 600], ["offsetWidth", 800]] as const) {
    const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, property)!.get!;
    vi.spyOn(HTMLElement.prototype, property, "get").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("chat-scroll") ? size : original.call(this);
    });
  }
}
