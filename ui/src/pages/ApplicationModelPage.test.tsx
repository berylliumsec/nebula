import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ApplicationModelPage } from "./ApplicationModelPage";
import { RelationshipEditor } from "./ApplicationModelEditors";
import { blankClaim, parseProperty, type Graph } from "./applicationModelTypes";
const request = vi.hoisted(() => vi.fn());
const draft = vi.hoisted(() => vi.fn());
const api = { request };
vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => ({ api, engagement: { id: "project", name: "Site A" } }),
}));
vi.mock("../state/WorkbenchDraftContext", () => ({
  useWorkbenchDrafts: () => ({ requestNebulaDraft: draft }),
}));
vi.mock("../components/PageHeader", () => ({
  PageHeader: ({ title }: { title: string }) => <h1>{title}</h1>,
}));
const graph: Graph = {
  project_id: "project",
  revision: 0,
  objects: [],
  relationships: [],
  schema: {
    categories: [{ name: "Structure", purpose: "Pages" }],
    types: [
      {
        name: "Page",
        label: "Page",
        description: "A page",
        category: "Structure",
        properties: [],
        identity_hints: [],
        outgoing_relationships: [],
        incoming_relationships: [],
      },
    ],
    relationships: [],
  },
};
beforeEach(() => {
  request.mockReset();
});
it("preserves a legacy relationship meaning when editing its claim", () => {
  const objects = ["a", "b"].map(id => ({id, label: id,
    authentication_context: "anonymous", classification: blankClaim("Page"), properties: {}, revision: 1}));
  const edge = {id: "old-link", type: "links_to", source: "a", target: "b", claim: blankClaim(true)};
  const saved = vi.fn();
  const legacy: Graph = {...graph, objects, relationships: [edge], schema: {...graph.schema,
    relationships: [{name: "links_to", label: "Links to (legacy)", description: "Historic navigation", source_types: ["Page"], target_types: ["Page"], legacy: true}]}};
  render(<RelationshipEditor graph={legacy} item={edge} evidence={[]} save={saved} cancel={() => {}} busy={false} />);
  expect(screen.getByRole("combobox", {name: "Relationship"})).toHaveValue("links_to");
  fireEvent.click(screen.getByRole("button", {name: "Save relationship"}));
  expect(saved.mock.calls[0][0][0].type).toBe("links_to");
});
describe("application graph workflow", () => {
  it("expands empty relationships and retains an editor draft across restore", async () => {
    request.mockImplementation(async (url: string) => url.includes("/evidence")
      ? { evidence: [], next_offset: null } : graph);
    render(<MemoryRouter><ApplicationModelPage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Expand relationships" }));
    expect(screen.getByRole("dialog", { name: "Project relationships" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore relationships" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Expand relationships" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Add object" }));
    const label = await screen.findByLabelText("Object label");
    fireEvent.change(label, { target: { value: "Unsent draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Expand model inspector" }));
    expect(screen.getByLabelText("Object label")).toBe(label);
    fireEvent.click(screen.getByRole("button", { name: "Restore model inspector" }));
    expect(screen.getByLabelText("Object label")).toBe(label);
    expect(label).toHaveValue("Unsent draft");
    expect(document.body.style.overflow).toBe("");
  });
  it("keeps the draft revision when a background refresh sees another writer", async () => {
    let revision = 0;
    request.mockImplementation(
      async (url: string, options?: { method?: string }) => {
        if (url.includes("/evidence"))
          return { evidence: [], next_offset: null };
        if (options?.method === "POST") throw Error("Revision conflict");
        return { ...graph, revision };
      },
    );
    render(
      <MemoryRouter>
        <ApplicationModelPage />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Add object" }));
    fireEvent.change(screen.getByLabelText("Object label"), {
      target: { value: "Retained draft" },
    });
    revision = 1;
    fireEvent(window, new Event("online"));
    await screen.findByText("Revision 1");
    fireEvent.click(screen.getByRole("button", { name: "Save object" }));
    await screen.findByRole("alert");
    const write = request.mock.calls.find((c) => c[1]?.method === "POST");
    expect(JSON.parse(write![1].body).expected_revision).toBe(0);
    expect(screen.getByLabelText("Object label")).toHaveValue("Retained draft");
  });
  it("opens model questions with graph context in the existing assistant", async () => {
    request.mockResolvedValue(graph);
    render(
      <MemoryRouter>
        <ApplicationModelPage />
      </MemoryRouter>,
    );
    fireEvent.change(await screen.findByLabelText("Model question"), {
      target: { value: "What supports this claim?" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Discuss with assistant" }),
    );
    expect(draft).toHaveBeenCalledWith(
      expect.objectContaining({
        sourceKind: "application_model",
        sourceId: "project",
        text: expect.stringContaining("What supports this claim?"),
      }),
      "chat",
    );
  });

  it("offers object creation without requiring a browser session or opaque identifier", async () => {
    request.mockResolvedValue(graph);
    render(
      <MemoryRouter>
        <ApplicationModelPage />
      </MemoryRouter>,
    );
    expect(
      await screen.findByText("Build a model from evidence"),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Add object" })).toBeEnabled();
    expect(screen.queryByText("Solver")).not.toBeInTheDocument();
  });
  it("retains a failed edit and retries the identical transaction", async () => {
    let attempts = 0;
    request.mockImplementation(
      async (url: string, options?: { method?: string; body?: string }) => {
        if (url.includes("/evidence"))
          return { evidence: [], next_offset: null };
        if (options?.method === "POST") {
          if (++attempts === 1) throw Error("Disconnected");
          return { revision: 1, changed_ids: ["page"] };
        }
        return graph;
      },
    );
    render(
      <MemoryRouter>
        <ApplicationModelPage />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Add object" }));
    fireEvent.change(screen.getByLabelText("Object label"), {
      target: { value: "Account" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save object" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "draft is retained",
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(attempts).toBe(2));
    const writes = request.mock.calls.filter((c) => c[1]?.method === "POST");
    expect(writes[0][1].body).toEqual(writes[1][1].body);
  });
  it("recovers stale selection without hiding the project", async () => {
    request.mockResolvedValue({
      ...graph,
      objects: [
        {
          id: "a",
          label: "Account",
          authentication_context: "anonymous",
          classification: blankClaim("Page"),
          properties: {},
          revision: 1,
        },
      ],
    });
    render(
      <MemoryRouter initialEntries={["/?object=missing&collection=old"]}>
        <ApplicationModelPage />
      </MemoryRouter>,
    );
    expect(
      await screen.findByRole("button", { name: "Clear selection" }),
    ).toBeVisible();
    expect(screen.getByText(/older link/)).toBeVisible();
  });
  it("rejects inexact integer edits and retains false", () => {
    const prop = {
      name: "count",
      kind: "integer" as const,
      description: "Count",
      choices: [],
    };
    expect(() => parseProperty(prop, "9007199254740993")).toThrow();
    expect(parseProperty({ ...prop, kind: "boolean" }, "false")).toBe(false);
  });
});

it("loads category pages from Core instead of transferring the full graph", async () => {
  request.mockImplementation(async (url: string) => {
    const params = new URLSearchParams(url.split("?")[1] ?? "");
    const offset = Number(params.get("offset") ?? 0);
    const active = params.get("category") === "Structure";
    const objects = active
      ? Array.from({ length: 20 }, (_, i) => ({
          id: `page-${offset + i}`,
          label: `Page ${offset + i}`,
          classification: {
            value: "Page",
            status: "hypothesized",
            evidence: [],
          },
          authentication_context: "anonymous",
          properties: {},
          revision: 1,
        }))
      : [];
    return {
      ...graph,
      object_total: 2000,
      category_counts: { Structure: 2000 },
      effective_category: active ? "Structure" : "",
      outline_objects: objects,
      outline_offset: offset,
      outline_total: active ? 2000 : 0,
      objects,
      map_objects: [],
      map_relationships: [],
      related_total: 0,
      listed_relationships: [],
    };
  });
  const { container } = render(
    <MemoryRouter>
      <ApplicationModelPage />
    </MemoryRouter>,
  );
  await screen.findByRole("heading", { name: "Explore the model" });
  expect(
    screen.queryByText("Build a model from evidence"),
  ).not.toBeInTheDocument();
  const details = container.querySelector(".am-category") as HTMLDetailsElement;
  details.open = true;
  fireEvent(details, new Event("toggle"));
  await waitFor(() =>
    expect(container.querySelectorAll(".am-outline .am-object")).toHaveLength(
      20,
    ),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Next Structure objects" }),
  );
  await screen.findByRole("button", { name: /Page 20.*Page/ });
  expect(
    request.mock.calls.some(
      ([url]) => url.includes("/view?") && url.includes("offset=20"),
    ),
  ).toBe(true);
  expect(request.mock.calls.some(([url]) => url.endsWith("/graph"))).toBe(
    false,
  );
});
