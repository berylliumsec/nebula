import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { matchesUpstreamQuery, parseUpstreamAllowlist, UpstreamProviderPicker, upstreamCatalog, upstreamLocation } from "./UpstreamProviderPicker";

const catalog = [
  { slug: "anthropic", name: "Anthropic", headquarters: "US" },
  { slug: "google-vertex", name: "Google Vertex" },
  { slug: "siliconflow", name: "SiliconFlow", headquarters: "SG", datacenters: ["US"] },
  { slug: "baidu", name: "Baidu", headquarters: "CN" },
  { slug: "venus", name: "Venus Cloud", headquarters: "DE" },
];

describe("UpstreamProviderPicker", () => {
  it("reads the persisted OpenRouter directory with locations and ignores malformed rows", () => {
    expect(upstreamCatalog({ openrouter_provider_catalog: [...catalog, { slug: 3 }, "x"] })).toEqual(catalog);
    expect(upstreamCatalog(undefined)).toEqual([]);
  });

  it("describes where a provider serves from, preferring listed datacenters", () => {
    expect(upstreamLocation(catalog[2])).toBe("US");
    expect(upstreamLocation(catalog[0])).toBe("US HQ");
    expect(upstreamLocation(catalog[1])).toBe("");
  });

  it("matches country codes exactly and names or slugs by substring", () => {
    const us = catalog.filter((item) => matchesUpstreamQuery(item, "us")).map((item) => item.slug);
    expect(us).toEqual(["anthropic", "siliconflow", "venus"]);
    expect(catalog.filter((item) => matchesUpstreamQuery(item, "vert")).map((item) => item.slug)).toEqual(["google-vertex"]);
    expect(matchesUpstreamQuery(catalog[3], "")).toBe(true);
  });

  it("parses JSON allowlists as slug arrays or OpenRouter's only shape", () => {
    expect(parseUpstreamAllowlist('[" GMICloud ", "together", "together"]')).toEqual({ slugs: ["gmicloud", "together"] });
    expect(parseUpstreamAllowlist('{"only": ["deepinfra"]}')).toEqual({ slugs: ["deepinfra"] });
    expect(parseUpstreamAllowlist("")).toEqual({ slugs: [] });
    expect(parseUpstreamAllowlist("[gmicloud")).toEqual({ error: "This is not valid JSON." });
    expect(parseUpstreamAllowlist("[1]")).toEqual({ error: "Every entry must be a provider slug string." });
    expect(parseUpstreamAllowlist('"gmicloud"')).toHaveProperty("error");
    expect(parseUpstreamAllowlist('["not a slug"]')).toEqual({ error: '"not a slug" is not a valid provider slug.' });
  });

  it("defaults to any provider, toggles the allowlist and removes chips", () => {
    const onChange = vi.fn();
    const { rerender } = render(<UpstreamProviderPicker catalog={catalog} selected={[]} query="" onQuery={vi.fn()} onChange={onChange} />);
    expect(screen.getByText("Any provider")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Anthropic, US HQ" }));
    expect(onChange).toHaveBeenLastCalledWith(["anthropic"]);

    rerender(<UpstreamProviderPicker catalog={catalog} selected={["anthropic", "baidu"]} query="" onQuery={vi.fn()} onChange={onChange} />);
    expect(screen.getByText("2 allowed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove Baidu" }));
    expect(onChange).toHaveBeenLastCalledWith(["anthropic"]);
    fireEvent.click(screen.getByRole("button", { name: "Allow any provider" }));
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  it("keeps saved choices visible before the directory loads and filters by search", () => {
    const { rerender } = render(<UpstreamProviderPicker catalog={[]} selected={["deepinfra"]} query="" onQuery={vi.fn()} onChange={vi.fn()} directoryState="loading" />);
    expect(screen.getByRole("checkbox", { name: "deepinfra" })).toBeChecked();

    rerender(<UpstreamProviderPicker catalog={catalog} selected={[]} query="vert" onQuery={vi.fn()} onChange={vi.fn()} />);
    expect(screen.queryByRole("checkbox", { name: /Anthropic/ })).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Google Vertex" })).toBeInTheDocument();
    expect(screen.getByText("1 of 5 providers")).toBeInTheDocument();
  });

  it("offers a retry when the directory fails to load", () => {
    const onReload = vi.fn();
    render(<UpstreamProviderPicker catalog={[]} selected={[]} query="" onQuery={vi.fn()} onChange={vi.fn()} directoryState="failed" onReloadDirectory={onReload} />);
    expect(screen.getByText("OpenRouter's provider list could not be loaded.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onReload).toHaveBeenCalledOnce();
  });

  it("edits the allowlist as JSON, applying only valid input", () => {
    const onChange = vi.fn();
    render(<UpstreamProviderPicker catalog={catalog} selected={["anthropic"]} query="" onQuery={vi.fn()} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    const editor = screen.getByRole("textbox", { name: "Upstream provider allowlist JSON" });
    expect(JSON.parse((editor as HTMLTextAreaElement).value)).toEqual(["anthropic"]);

    fireEvent.change(editor, { target: { value: '["anthropic",' } });
    expect(screen.getByRole("alert")).toHaveTextContent("This is not valid JSON.");
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.change(editor, { target: { value: '{"only": ["siliconflow", "gmicloud"]}' } });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(onChange).toHaveBeenLastCalledWith(["siliconflow", "gmicloud"]);
  });
});
