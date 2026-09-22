import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { SearchableModelPicker, type ModelPickerOption } from "./SearchableModelPicker";

const options: ModelPickerOption[] = [
  { id: "openai/gpt-sol", label: "OpenAI: GPT Sol (openai/gpt-sol) · 1,050,000 context", group: "allowed" },
  { id: "deepseek/deepseek-flash", label: "DeepSeek Flash (deepseek/deepseek-flash) · 1,048,576 context", searchText: "Fast low latency model", group: "more" },
  { id: "x-ai/grok-4.7", label: "SpaceXAI: Grok 4.7 (x-ai/grok-4.7) · 500,000 context", group: "allowed" },
];

it("searches large model lists by name or ID and keeps the selected model visible", () => {
  const onSelect = vi.fn();
  render(<SearchableModelPicker options={options} value="openai/gpt-sol" providerName="OpenRouter" onSelect={onSelect} />);
  const trigger = screen.getByRole("button", { name: /^Chat model:/ });
  expect(trigger).toHaveTextContent("GPT Sol");
  fireEvent.click(trigger);
  const search = screen.getByRole("combobox", { name: "Search models" });
  expect(search).toHaveFocus();
  expect(screen.getByRole("option", { name: /GPT Sol/ })).toHaveAttribute("aria-selected", "true");
  fireEvent.change(search, { target: { value: "deepseek/deepseek-flash" } });
  const results = screen.getByRole("listbox", { name: "Models" });
  expect(within(results).getAllByRole("option")).toHaveLength(1);
  expect(within(results).getByRole("option", { name: /DeepSeek Flash/ })).toHaveTextContent("Add to OpenRouter's allowed models");
  fireEvent.change(search, { target: { value: "low latency" } });
  expect(within(results).getAllByRole("option")).toHaveLength(1);
  fireEvent.keyDown(search, { key: "Enter" });
  expect(onSelect).toHaveBeenCalledWith("deepseek/deepseek-flash");
  expect(trigger).toHaveFocus();
  expect(screen.queryByRole("listbox")).toBeNull();
});

it("supports keyboard navigation, empty results, and Escape without changing models", () => {
  const onSelect = vi.fn();
  render(<SearchableModelPicker options={options} value="openai/gpt-sol" providerName="OpenRouter" onSelect={onSelect} />);
  const trigger = screen.getByRole("button", { name: /^Chat model:/ });
  fireEvent.keyDown(trigger, { key: "ArrowDown" });
  const search = screen.getByRole("combobox", { name: "Search models" });
  fireEvent.change(search, { target: { value: "missing model" } });
  expect(screen.getByText("No matching models")).toBeVisible();
  fireEvent.change(search, { target: { value: "grok" } });
  expect(screen.getByRole("option", { name: /Grok 4.7/ })).toBeVisible();
  fireEvent.keyDown(search, { key: "Escape" });
  expect(trigger).toHaveFocus();
  expect(onSelect).not.toHaveBeenCalled();
});

it("moves through results with arrow keys and selects the highlighted model", () => {
  const onSelect = vi.fn();
  render(<SearchableModelPicker options={options} value="openai/gpt-sol" providerName="OpenRouter" onSelect={onSelect} />);
  fireEvent.keyDown(screen.getByRole("button", { name: /^Chat model:/ }), { key: "ArrowDown" });
  const search = screen.getByRole("combobox", { name: "Search models" });
  const first = search.getAttribute("aria-activedescendant");
  fireEvent.keyDown(search, { key: "ArrowDown" });
  expect(search.getAttribute("aria-activedescendant")).not.toBe(first);
  fireEvent.keyDown(search, { key: "Enter" });
  expect(onSelect).toHaveBeenCalledWith("deepseek/deepseek-flash");
});
