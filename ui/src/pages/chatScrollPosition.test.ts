import {expect, it} from "vitest";
import {followsChatBottom} from "./chatScrollPosition";
const bottom = {scrollTop: 600, scrollHeight: 1000, clientHeight: 400};
it("keeps following when the final message grows after initial scrolling", () => {
  expect(followsChatBottom(bottom, {...bottom, scrollHeight: 1400}, true)).toBe(true);
  expect(followsChatBottom(undefined, {...bottom, scrollTop: 0}, true)).toBe(true);
});
it("preserves intentional reading positions and allows returning to latest", () => {
  expect(followsChatBottom(bottom, {...bottom, scrollTop: 150}, true)).toBe(false);
  expect(followsChatBottom(bottom, {...bottom, scrollHeight: 1400}, false)).toBe(false);
  expect(followsChatBottom(bottom, bottom, false)).toBe(true);
});
it("keeps following when the composer changes viewport height", () => {
  expect(followsChatBottom(bottom, {...bottom, clientHeight: 300}, true)).toBe(true);
});
