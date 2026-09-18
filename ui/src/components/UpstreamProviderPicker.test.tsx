import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { UpstreamProviderPicker, upstreamCatalog } from "./UpstreamProviderPicker";

const catalog = [
  { slug: "anthropic", name: "Anthropic" },
  { slug: "google-vertex", name: "Google Vertex" },
];

describe("UpstreamProviderPicker", () => {
  it("reads the persisted OpenRouter directory and ignores malformed rows", () => {
    expect(upstreamCatalog({ openrouter_provider_catalog: [...catalog, { slug: 3 }, "x"] })).toEqual(catalog);
    expect(upstreamCatalog(undefined)).toEqual([]);
  });

  it("defaults to any provider and toggles the allowlist", () => {
    const onChange = vi.fn();
    const { rerender } = render(<UpstreamProviderPicker catalog={catalog} selected={[]} query="" onQuery={vi.fn()} onChange={onChange} />);
    expect(screen.getByText("Upstream providers · Any")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Anthropic" }));
    expect(onChange).toHaveBeenLastCalledWith(["anthropic"]);

    rerender(<UpstreamProviderPicker catalog={catalog} selected={["anthropic"]} query="" onQuery={vi.fn()} onChange={onChange} />);
    expect(screen.getByText("Upstream providers · 1 allowed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Allow any provider" }));
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  it("keeps saved choices visible before the directory loads and filters by search", () => {
    const { rerender } = render(<UpstreamProviderPicker catalog={[]} selected={["deepinfra"]} query="" onQuery={vi.fn()} onChange={vi.fn()} />);
    expect(screen.getByRole("checkbox", { name: "deepinfra" })).toBeChecked();

    rerender(<UpstreamProviderPicker catalog={catalog} selected={[]} query="vert" onQuery={vi.fn()} onChange={vi.fn()} />);
    expect(screen.queryByRole("checkbox", { name: "Anthropic" })).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Google Vertex" })).toBeInTheDocument();
  });
});
