import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { StructuredResultDashboard } from "./StructuredResultDashboard";

vi.mock("../../diagnostics", () => ({
  logCaughtDiagnostic: vi.fn(),
  DiagnosticErrorNotice: ({ error }: { error: string }) => <div role="alert">{error}</div>,
}));

const show = (value: unknown, hints?: unknown) =>
  render(<StructuredResultDashboard value={value} hints={hints} title="Result" />);

describe("exploring a previously unseen result", () => {
  it("leads with top-level values and collection counts, with no schema registered", async () => {
    show({
      name: "edge-01",
      status: "quarantined-unknown",
      port: 443,
      note: null,
      hosts: [{ ip: "10.0.0.1" }, { ip: "10.0.0.2" }],
    });
    expect(screen.getByText("edge-01")).toBeInTheDocument();
    // An unrecognised status renders as its own text, not as a pass or a fail.
    expect(screen.getByText("quarantined-unknown")).toBeInTheDocument();
    expect(screen.getByText("443")).toBeInTheDocument();
    expect(screen.getByText("null")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /hosts/ })).toHaveTextContent("array · 2 items");
  });

  it("offers a table for a homogeneous collection and sorts and filters it", async () => {
    const user = userEvent.setup();
    show({ hosts: [{ ip: "10.0.0.2", port: 80 }, { ip: "10.0.0.1", port: 443 }] });
    await user.click(screen.getByRole("tab", { name: "Table" }));

    const header = screen.getByRole("columnheader", { name: /^ip/ });
    expect(header).toHaveAttribute("aria-sort", "none");
    await user.click(within(header).getByRole("button"));
    expect(screen.getByRole("columnheader", { name: /^ip/ })).toHaveAttribute("aria-sort", "ascending");
    const [firstRow] = screen.getAllByRole("row").slice(1);
    expect(firstRow).toHaveTextContent("10.0.0.1");

    await user.type(screen.getByRole("searchbox", { name: /Filter/ }), "10.0.0.2");
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(screen.getByText(/Rows 1–1 of 1/)).toBeInTheDocument();
  });

  it("reports a field a row does not carry instead of showing an empty cell", async () => {
    const user = userEvent.setup();
    show({ rows: [{ a: 1, b: 2 }, { a: 3 }] });
    await user.click(screen.getByRole("tab", { name: "Table" }));
    expect(screen.getAllByText("not present")).toHaveLength(1);
  });

  it("always provides a tree, and searches names and values in it", async () => {
    const user = userEvent.setup();
    show({ outer: { inner: { needle: "findme" } }, other: 1 });
    await user.click(screen.getByRole("tab", { name: "Tree" }));
    expect(screen.getByRole("button", { name: /^outer/ })).toBeInTheDocument();

    await user.type(screen.getByRole("searchbox", { name: /Search property names/ }), "findme");
    expect(await screen.findByText(/1 match/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^needle/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^other/ })).not.toBeInTheDocument();
  });

  it("always provides the unmodified raw result", async () => {
    const user = userEvent.setup();
    show({ spaced: "  keep  ", big: 9007199254740993 });
    await user.click(screen.getByRole("tab", { name: "Raw" }));
    const raw = screen.getByLabelText("Raw result text");
    // Asserted on the exact text: the matcher's whitespace normalisation would
    // hide the very thing this view exists to preserve.
    expect(raw.textContent).toContain('"spaced": "  keep  "');
    expect(raw.textContent).toContain('"big": 9007199254740992');
    expect(screen.getByText(/exactly as it was published/)).toBeInTheDocument();
  });

  it("opens the relationship view only for a recognisable graph, with a table beside it", async () => {
    const user = userEvent.setup();
    show({ nodes: [{ id: "a" }, { id: "b" }], edges: [{ source: "a", target: "b" }] });
    await user.click(screen.getByRole("tab", { name: "Relationships" }));

    expect(await screen.findByRole("group", { name: /Relationship graph/ })).toBeInTheDocument();
    // The picture is optional; the relationships are always readable as rows.
    const table = screen.getByRole("table", { name: /Relationships in/ });
    expect(within(table).getByRole("cell", { name: "a" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Hide the picture" }));
    expect(screen.queryByRole("group", { name: /Relationship graph/ })).not.toBeInTheDocument();
    expect(within(table).getByRole("cell", { name: "b" })).toBeInTheDocument();
  });

  it("does not offer relationships for something that only resembles a graph", () => {
    show({ nodes: [{ id: "a" }], edges: [{ from: "a" }] });
    expect(screen.queryByRole("tab", { name: "Relationships" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Tree" })).toBeInTheDocument();
  });

  it("renders unsafe URLs and embedded markup as text", async () => {
    const user = userEvent.setup();
    show({ link: "javascript:alert(1)", safe: "https://example.com/ok", markup: "<img src=x onerror=alert(1)>" });
    expect(screen.queryByRole("link", { name: "javascript:alert(1)" })).not.toBeInTheDocument();
    expect(screen.getByText("javascript:alert(1)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "https://example.com/ok" })).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
    await user.click(screen.getByRole("tab", { name: "Raw" }));
    expect(document.querySelector("img")).toBeNull();
  });

  it("opens one inspector from any view and returns focus when it closes", async () => {
    const user = userEvent.setup();
    show({ hosts: [{ ip: "10.0.0.1", tags: ["edge"] }] });
    const opener = screen.getByRole("button", { name: /^hosts/ });
    await user.click(opener);

    const inspector = screen.getByRole("complementary", { name: "hosts" });
    expect(within(inspector).getByText("$.hosts")).toBeInTheDocument();
    expect(within(inspector).getAllByText("array · 1 item").length).toBeGreaterThan(0);
    expect(document.activeElement).toHaveTextContent("hosts");

    await user.click(screen.getByRole("button", { name: "Close detail" }));
    expect(screen.queryByText("$.hosts")).not.toBeInTheDocument();
    // Focus is restored on the frame after the inspector unmounts.
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });

  it("routes from the inspector back to the value's place in the raw result", async () => {
    const user = userEvent.setup();
    show({ outer: { target: "here" } });
    await user.click(screen.getByRole("button", { name: /^outer/ }));
    await user.click(screen.getByRole("button", { name: /Show in raw result/ }));
    expect(screen.getByLabelText("Raw result text").textContent).toContain('"target": "here"');
  });

  it("keeps a large collection usable without rendering all of it", async () => {
    const user = userEvent.setup();
    const rows = Array.from({ length: 5_000 }, (_item, index) => ({ index, name: `row-${index}` }));
    show({ rows });
    await user.click(screen.getByRole("tab", { name: "Table" }));
    expect(screen.getAllByRole("row")).toHaveLength(26);
    expect(screen.getByText(/Rows 1–25 of 5,000/)).toBeInTheDocument();
    expect(screen.getByText(/Columns were inferred from the first 200 of 5,000 rows/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText(/Rows 26–50 of 5,000/)).toBeInTheDocument();
  });

  it("handles empty, null-heavy and scalar results without a specialised view", () => {
    const { unmount } = show({});
    expect(screen.getByText(/empty object/)).toBeInTheDocument();
    unmount();

    const scalar = show("just text");
    expect(screen.getByText(/single string value/)).toBeInTheDocument();
    scalar.unmount();

    show({ a: null, b: null });
    expect(screen.getAllByText("null")).toHaveLength(2);
    expect(screen.queryByRole("tab", { name: "Table" })).not.toBeInTheDocument();
  });

  it("applies optional hints and ignores invalid ones without losing the result", async () => {
    const user = userEvent.setup();
    show(
      { secret: "hunter2", name: "edge-01", rows: [{ a: 1, b: 2 }] },
      { fieldOrder: ["name"], redactFields: ["secret"], labels: { name: "Host name" }, formats: { name: "hologram" } },
    );
    expect(screen.getByText("Host name")).toBeInTheDocument();
    expect(screen.queryByText("hunter2")).not.toBeInTheDocument();
    expect(screen.getByText(/1 presentation hint was not used/)).toBeInTheDocument();

    // Redaction masks a value; it never removes it.
    await user.click(screen.getByRole("button", { name: /Reveal/ }));
    expect(screen.getByText("hunter2")).toBeInTheDocument();
  });
});
