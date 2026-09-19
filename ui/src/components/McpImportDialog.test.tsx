import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { McpImportEntry, McpImportReport } from "../api/types";
import { DialogProvider } from "./DialogSystem";
import { MCP_FORMAT_GUIDE_URL, McpImportDialog, describeJsonError } from "./McpImportDialog";

vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn(), DiagnosticErrorNotice: ({ error }: { error: string }) => <div role="alert">{error}</div> }));

const config = {
  mcpServers: {
    burp: { command: "npx", args: ["-y", "burp-mcp"], env: { BURP_API_KEY: "${BURP_API_KEY}", SHODAN_TOKEN: "literal" }, alwaysAllow: ["scan"] },
    github: { command: "npx", args: ["-y", "@modelcontextprotocol/server-github"] },
    recon: { command: "recon-mcp" },
  },
};

interface Choices { defaults?: { enabled: boolean; defaultApproval: McpImportEntry["defaultApproval"] }; trustLocalPrograms?: boolean }

/** Mimics Core's dry run: burp is new, github's args changed, intel matches. */
function report({ defaults, trustLocalPrograms = false }: Choices = {}, dryRun = true): McpImportReport {
  const enable = defaults?.enabled ?? false;
  return {
    dryRun,
    created: 1,
    updated: 1,
    unchanged: 1,
    replaced: 0,
    skipped: 0,
    invalid: 1,
    entries: [
      {
        sourceName: "burp", name: "burp", action: "create", transport: "stdio", command: "/usr/bin/npx", arguments: ["-y", "burp-mcp"],
        secrets: [
          { target: "env BURP_API_KEY", source: "environment", reference: "env:BURP_API_KEY" },
          { target: "env SHODAN_TOKEN", source: "vault" },
        ],
        changes: [], enabled: enable && trustLocalPrograms, defaultApproval: defaults?.defaultApproval ?? "risk_based", needsTrust: enable, needsProbe: enable && trustLocalPrograms,
        warnings: ["alwaysAllow: tool approvals were not imported; review them in Nebula", "resolved npx to /usr/bin/npx"],
      },
      {
        sourceName: "github", name: "github", action: "update", transport: "stdio", command: "/usr/bin/npx", arguments: ["-y", "server-github@2"], secrets: [],
        changes: [{ field: "args", before: "-y server-github", after: "-y server-github@2" }, { field: "env LOG_LEVEL", before: "debug" }],
        enabled: trustLocalPrograms, defaultApproval: "risk_based", needsTrust: true, needsProbe: trustLocalPrograms, warnings: [],
      },
      { sourceName: "intel", name: "intel", action: "unchanged", transport: "streamable_http", url: "https://mcp.example.test/mcp", arguments: [], secrets: [{ target: "Authorization bearer token", source: "environment", reference: "env:INTEL_TOKEN" }], changes: [], enabled: true, defaultApproval: "ask", needsTrust: false, needsProbe: false, warnings: [] },
      { sourceName: "recon", name: "recon", action: "invalid", arguments: [], secrets: [], changes: [], enabled: false, needsTrust: false, needsProbe: false, warnings: [], error: "'recon-mcp' is not installed on the Nebula host PATH; install it or use an absolute path" },
    ],
  };
}

const api = { importMcpServers: vi.fn(), mcpServerSchema: vi.fn() };

function renderDialog(onImported = vi.fn(), onClose = vi.fn()) {
  render(<DialogProvider><McpImportDialog api={api as unknown as ApiClient} onClose={onClose} onImported={onImported} /></DialogProvider>);
  return { dialog: screen.getByRole("dialog"), onImported, onClose };
}

async function paste(dialog: HTMLElement, text: string) {
  const field = within(dialog).getByRole("textbox", { name: "Configuration" });
  await userEvent.click(field);
  await userEvent.paste(text);
}

describe("MCP import dialog", () => {
  beforeEach(() => { vi.resetAllMocks(); });

  it("shows the example and format links until something is pasted", async () => {
    const { dialog } = renderDialog();
    const example = within(dialog).getByText("Example").closest("details")!;
    expect(example).toHaveAttribute("open");
    expect(example.querySelector("code")?.textContent).toContain('"mcpServers"');
    expect(within(dialog).getByRole("link", { name: "Format guide" })).toHaveAttribute("href", MCP_FORMAT_GUIDE_URL);
    expect(within(dialog).getByRole("button", { name: "Preview" })).toBeDisabled();

    await paste(dialog, JSON.stringify(config));
    expect(example).not.toHaveAttribute("open");
    expect(within(dialog).getByRole("button", { name: "Preview" })).toBeEnabled();
  });

  it("explains invalid JSON with its location and sends nothing", async () => {
    const { dialog } = renderDialog();
    await paste(dialog, '{\n  "mcpServers": {\n    "a": {}\n    "b": {}\n  }\n}');
    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));

    const error = within(dialog).getByRole("alert");
    expect(error).toHaveTextContent(/^Not valid JSON: line 4, column 5/);
    expect(error).toHaveTextContent("Nothing was imported.");
    expect(within(dialog).getByRole("textbox", { name: "Configuration" })).toHaveAttribute("aria-invalid", "true");
    expect(within(dialog).getByRole("button", { name: "Preview" })).toBeDisabled();
    expect(api.importMcpServers).not.toHaveBeenCalled();

    await paste(dialog, " ");
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("rejects JSON that is not an object before calling Core", async () => {
    const { dialog } = renderDialog();
    await paste(dialog, "[1, 2]");
    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent('"mcpServers" or "servers"');
    expect(api.importMcpServers).not.toHaveBeenCalled();
  });

  it("previews what differs, re-previews each choice, then saves with those choices", async () => {
    api.importMcpServers.mockImplementation(async (request) => report(request, request.dryRun));
    const { dialog, onImported } = renderDialog();
    await paste(dialog, JSON.stringify(config));
    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));

    expect(api.importMcpServers).toHaveBeenLastCalledWith({
      config, dryRun: true, onConflict: "update", sourceName: "Pasted configuration",
      defaults: { enabled: false, defaultApproval: "risk_based" }, trustLocalPrograms: false,
    });
    const rows = within(dialog).getByRole("list", { name: "Servers in this file" });
    const [burp, github, intel, recon] = within(rows).getAllByRole("listitem").filter((item) => item.parentElement === rows);
    expect(burp).toHaveTextContent("New");
    expect(burp).toHaveTextContent("Disabled · Risk-based");
    expect(burp).toHaveTextContent("BURP_API_KEY from Nebula's environment · SHODAN_TOKEN to credential vault");
    expect(within(burp).getByText("2 notes").closest("details")).not.toHaveAttribute("open");
    expect(github).toHaveTextContent("Update");
    expect(within(github).getByText("2 changes").closest("details")).toHaveAttribute("open");
    expect(github).toHaveTextContent("args -y server-github →becomes -y server-github@2");
    expect(github).toHaveTextContent("env LOG_LEVEL debug →becomes removed");
    expect(github).toHaveTextContent("Disabled until trusted");
    expect(github).toHaveTextContent("Its launch settings changed, so github is untrusted again.");
    expect(intel).toHaveTextContent("Unchanged");
    expect(intel).toHaveTextContent("Matches the saved server. Nothing to save.");
    expect(intel).not.toHaveTextContent("Bearer token");
    expect(recon).toHaveTextContent("Can't import");
    for (const chip of ["1 new", "1 update", "1 unchanged", "1 can't import"]) expect(within(dialog).getByText(chip)).toBeVisible();
    expect(within(dialog).getByRole("checkbox", { name: /Trust github to run on this Core/ })).not.toBeChecked();
    const save = within(dialog).getByRole("button", { name: "Save 2 servers" });

    await userEvent.click(within(dialog).getByRole("checkbox", { name: /Enable after import/ }));
    await waitFor(() => expect(api.importMcpServers).toHaveBeenLastCalledWith(expect.objectContaining({ defaults: { enabled: true, defaultApproval: "risk_based" }, trustLocalPrograms: false })));
    const trust = await within(dialog).findByRole("checkbox", { name: /Trust 2 local programs to run on this Core/ });
    expect(trust.closest("label")).toHaveTextContent("burp and github are saved disabled");

    await userEvent.click(within(dialog).getByRole("radio", { name: "Ask" }));
    await waitFor(() => expect(api.importMcpServers).toHaveBeenLastCalledWith(expect.objectContaining({ defaults: { enabled: true, defaultApproval: "ask" } })));
    expect(within(dialog).getByRole("group", { name: "Tool approval" })).toHaveAccessibleDescription(/^Asks before every tool call\./);

    await userEvent.click(trust);
    await waitFor(() => expect(api.importMcpServers).toHaveBeenLastCalledWith(expect.objectContaining({ trustLocalPrograms: true })));
    await waitFor(() => expect(burp).toHaveTextContent("Enabled · Ask"));
    expect(github).toHaveTextContent("Stays enabled · Risk-based");
    expect(github).not.toHaveTextContent("untrusted again");

    await userEvent.click(save);
    await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1));
    expect(api.importMcpServers).toHaveBeenLastCalledWith({
      config, dryRun: false, onConflict: "update", sourceName: "Pasted configuration",
      defaults: { enabled: true, defaultApproval: "ask" }, trustLocalPrograms: true,
    });
    expect(onImported.mock.calls[0][1]).toBe("Pasted configuration");
  });

  it("asks for trust again when Enable changes which programs it covers", async () => {
    api.importMcpServers.mockImplementation(async (request) => report(request, request.dryRun));
    const { dialog } = renderDialog();
    await paste(dialog, JSON.stringify(config));
    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));
    await userEvent.click(await within(dialog).findByRole("checkbox", { name: /Trust github/ }));
    await waitFor(() => expect(api.importMcpServers).toHaveBeenLastCalledWith(expect.objectContaining({ trustLocalPrograms: true })));

    await userEvent.click(within(dialog).getByRole("checkbox", { name: /Enable after import/ }));
    await waitFor(() => expect(api.importMcpServers).toHaveBeenLastCalledWith(expect.objectContaining({ defaults: expect.objectContaining({ enabled: true }), trustLocalPrograms: false })));
    expect(await within(dialog).findByRole("checkbox", { name: /Trust 2 local programs/ })).not.toBeChecked();
  });

  it("offers nothing to save when every server already matches", async () => {
    const unchanged = report();
    const intel = unchanged.entries[2];
    api.importMcpServers.mockResolvedValue({ ...unchanged, created: 0, updated: 0, invalid: 0, unchanged: 1, entries: [intel] });
    const { dialog } = renderDialog();
    await paste(dialog, JSON.stringify(config));
    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));
    expect(await within(dialog).findByRole("button", { name: "Nothing to save" })).toBeDisabled();
    expect(within(dialog).queryByRole("group", { name: "New servers" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("checkbox", { name: /Trust/ })).not.toBeInTheDocument();
    expect(within(dialog).getByText(/Only what changed is saved/)).toBeVisible();
  });

  it("keeps the dialog open when nothing could be saved", async () => {
    api.importMcpServers.mockResolvedValueOnce(report()).mockResolvedValueOnce({ ...report({}, false), created: 0, updated: 0, invalid: 3 });
    const { dialog, onImported } = renderDialog();
    await paste(dialog, JSON.stringify(config));
    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save 2 servers" }));
    expect(await within(dialog).findByText(/No servers were saved/)).toBeVisible();
    expect(onImported).not.toHaveBeenCalled();
  });

  it("shows a Core rejection and returns to the source with text intact", async () => {
    api.importMcpServers.mockRejectedValueOnce(new Error('the file must contain an "mcpServers" (or VS Code "servers") object'));
    const { dialog } = renderDialog();
    await paste(dialog, '{"tools": {}}');
    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent('"mcpServers"');

    api.importMcpServers.mockResolvedValueOnce(report());
    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));
    await userEvent.click(await within(dialog).findByRole("button", { name: "Back" }));
    expect(within(dialog).getByRole("textbox", { name: "Configuration" })).toHaveValue('{"tools": {}}');
  });

  it("loads a chosen file and uses its name as the source", async () => {
    api.importMcpServers.mockResolvedValue(report());
    const { dialog } = renderDialog();
    const file = new File([JSON.stringify(config)], "claude_desktop_config.json", { type: "application/json" });
    await userEvent.upload(within(dialog).getByLabelText("Choose MCP configuration file"), file);
    await waitFor(() => expect(within(dialog).getByRole("textbox", { name: "Configuration" })).toHaveValue(JSON.stringify(config)));
    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));
    expect(api.importMcpServers).toHaveBeenLastCalledWith(expect.objectContaining({ sourceName: "claude_desktop_config.json" }));
    expect(within(dialog).getByText(/claude_desktop_config\.json · 4 servers/)).toBeVisible();
  });

  it("refuses oversized files without reading them", async () => {
    const { dialog } = renderDialog();
    const file = new File(["x".repeat(1024 * 1024 + 1)], "huge.json", { type: "application/json" });
    await userEvent.upload(within(dialog).getByLabelText("Choose MCP configuration file"), file);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("larger than 1 MB");
    expect(within(dialog).getByRole("textbox", { name: "Configuration" })).toHaveValue("");
  });

  it("downloads the schema from Core", async () => {
    api.mcpServerSchema.mockResolvedValue({ title: "Nebula MCP server configuration" });
    const createObjectURL = vi.fn(() => "blob:schema");
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const { dialog } = renderDialog();
    await userEvent.click(within(dialog).getByRole("button", { name: "Download schema" }));
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(api.mcpServerSchema).toHaveBeenCalledTimes(1);
    expect((click.mock.contexts[0] as HTMLAnchorElement).download).toBe("mcp-servers.schema.json");
    click.mockRestore();
  });
});

describe("describeJsonError", () => {
  const text = '{\n  "a": 1\n  "b": 2\n}';
  it.each([
    ["Expected ',' or '}' after property value in JSON at position 13 (line 3 column 3)", "line 3, column 3 — Expected ',' or '}' after property value"],
    ["Unexpected string in JSON at position 13", "line 3, column 3 — Unexpected string"],
    ["JSON.parse: expected ',' or '}' after property value in object at line 3 column 3 of the JSON data", "line 3, column 3 — expected ',' or '}' after property value"],
    ["JSON Parse error: Expected '}'", "Not valid JSON — Expected '}'"],
  ])("normalizes %s", (message, expected) => {
    expect(describeJsonError(text, new SyntaxError(message))).toContain(expected);
  });
});
