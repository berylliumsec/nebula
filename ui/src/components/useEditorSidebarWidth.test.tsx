import { act, renderHook } from "@testing-library/react";
import { beforeEach, expect, test } from "vitest";
import { useEditorSidebarWidth } from "./useEditorSidebarWidth";

beforeEach(() => localStorage.clear());

test("sidebar width rejects invalid stored preferences", () => {
  localStorage.setItem("nebula.editor.sidebar-width", "Infinity");
  const {result} = renderHook(useEditorSidebarWidth);
  expect(result.current.width).toBe(250);
});

test("sidebar width clamps changes and restores the device preference", () => {
  const {result, unmount} = renderHook(useEditorSidebarWidth);
  act(() => result.current.resize(9999));
  expect(result.current.width).toBe(600);
  act(() => result.current.resize(-4));
  expect(result.current.width).toBe(200);
  act(() => result.current.resize(386));
  expect(localStorage.getItem("nebula.editor.sidebar-width")).toBe("386");
  unmount();
  expect(renderHook(useEditorSidebarWidth).result.current.width).toBe(386);
});
