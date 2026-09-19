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
it("treats scrolling up during a streamed response as reader intent", () => {
  expect(followsChatBottom(bottom, {scrollTop: 100, scrollHeight: 1400, clientHeight: 400}, true)).toBe(false);
  expect(followsChatBottom(bottom, {scrollTop: 598, scrollHeight: 1400, clientHeight: 400}, true)).toBe(true);
});
it("keeps following when a status strip shrinks the viewport while a row is added", () => {
  // Recorded on a phone: the viewport lost 91px and a row added 178px in one frame,
  // and the browser moved scrollTop up 26px without any reader input.
  const before = {scrollTop: 3741, scrollHeight: 4151, clientHeight: 410};
  expect(followsChatBottom(before, {scrollTop: 3715, scrollHeight: 4329, clientHeight: 319}, true)).toBe(true);
  expect(followsChatBottom(before, {scrollTop: 3715, scrollHeight: 4329, clientHeight: 319}, false)).toBe(false);
});
