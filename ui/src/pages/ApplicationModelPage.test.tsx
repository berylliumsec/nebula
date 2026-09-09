import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ApplicationModelPage } from "./ApplicationModelPage";
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
describe("application graph workflow", () => {
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
