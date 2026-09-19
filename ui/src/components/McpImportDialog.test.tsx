import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { McpImportReport } from "../api/types";
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

function report(replace: boolean, dryRun = true): McpImportReport {
  return {
    dryRun,
    created: 1,
    replaced: replace ? 1 : 0,
    skipped: replace ? 0 : 1,
    invalid: 1,
    entries: [
      {
        sourceName: "burp", name: "burp", action: "create", transport: "stdio", command: "/usr/bin/npx", arguments: ["-y", "burp-mcp"],
        secrets: [
          { target: "env BURP_API_KEY", source: "environment", reference: "env:BURP_API_KEY" },
          { target: "env SHODAN_TOKEN", source: "vault" },
        ],
        warnings: ["alwaysAllow: tool approvals were not imported; review them in Nebula", "resolved npx to /usr/bin/npx"],
      },
      { sourceName: "github", name: "github", action: replace ? "replace" : "skip", transport: "stdio", command: "/usr/bin/npx", arguments: ["-y", "@modelcontextprotocol/server-github"], secrets: [], warnings: [] },
      { sourceName: "recon", name: "recon", action: "invalid", arguments: [], secrets: [], warnings: [], error: "'recon-mcp' is not installed on the Nebula host PATH; install it or use an absolute path" },
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

  it("previews rows from Core, re-previews on replace, then imports what it showed", async () => {
    api.importMcpServers.mockImplementation(async ({ onConflict, dryRun }) => report(onConflict === "replace", dryRun));
    const { dialog, onImported } = renderDialog();
    await paste(dialog, JSON.stringify(config));
    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));

    expect(api.importMcpServers).toHaveBeenLastCalledWith({ config, dryRun: true, onConflict: "skip", sourceName: "Pasted configuration" });
    const rows = within(dialog).getByRole("list", { name: "Servers in this file" });
    const [burp, github, recon] = within(rows).getAllByRole("listitem").filter((item) => item.parentElement === rows);
    expect(burp).toHaveTextContent("New");
    expect(burp).toHaveTextContent("/usr/bin/npx -y burp-mcp");
    expect(burp).toHaveTextContent("BURP_API_KEY from Nebula's environment · SHODAN_TOKEN to credential vault");
    expect(within(burp).getByText("2 notes").closest("details")).not.toHaveAttribute("open");
    expect(github).toHaveTextContent("Exists · skipped");
    expect(recon).toHaveTextContent("Can't import");
    expect(recon).toHaveTextContent("is not installed on the Nebula host PATH");
    expect(within(dialog).getByText("1 new")).toBeVisible();
    expect(within(dialog).getByText("1 can't import")).toBeVisible();
    const importButton = within(dialog).getByRole("button", { name: "Import 1 server" });

    await userEvent.click(within(dialog).getByRole("checkbox", { name: /Replace github/ }));
    await waitFor(() => expect(api.importMcpServers).toHaveBeenLastCalledWith(expect.objectContaining({ dryRun: true, onConflict: "replace" })));
    expect(await within(dialog).findByText("Replaces existing")).toBeVisible();
    expect(importButton).toHaveTextContent("Import 2 servers");

    await userEvent.click(importButton);
    await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1));
    expect(api.importMcpServers).toHaveBeenLastCalledWith({ config, dryRun: false, onConflict: "replace", sourceName: "Pasted configuration" });
    expect(onImported.mock.calls[0][1]).toBe("Pasted configuration");
  });

  it("keeps the dialog open when nothing could be saved", async () => {
    api.importMcpServers.mockResolvedValueOnce(report(false)).mockResolvedValueOnce({ ...report(false, false), created: 0, invalid: 2 });
    const { dialog, onImported } = renderDialog();
    await paste(dialog, JSON.stringify(config));
    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Import 1 server" }));
    expect(await within(dialog).findByText(/No servers were saved/)).toBeVisible();
    expect(onImported).not.toHaveBeenCalled();
  });

  it("shows a Core rejection and returns to the source with text intact", async () => {
    api.importMcpServers.mockRejectedValueOnce(new Error('the file must contain an "mcpServers" (or VS Code "servers") object'));
    const { dialog } = renderDialog();
    await paste(dialog, '{"tools": {}}');
    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent('"mcpServers"');

    api.importMcpServers.mockResolvedValueOnce(report(false));
    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));
    await userEvent.click(await within(dialog).findByRole("button", { name: "Back" }));
    expect(within(dialog).getByRole("textbox", { name: "Configuration" })).toHaveValue('{"tools": {}}');
  });

  it("loads a chosen file and uses its name as the source", async () => {
    api.importMcpServers.mockResolvedValue(report(false));
    const { dialog } = renderDialog();
    const file = new File([JSON.stringify(config)], "claude_desktop_config.json", { type: "application/json" });
    await userEvent.upload(within(dialog).getByLabelText("Choose MCP configuration file"), file);
    await waitFor(() => expect(within(dialog).getByRole("textbox", { name: "Configuration" })).toHaveValue(JSON.stringify(config)));
    await userEvent.click(within(dialog).getByRole("button", { name: "Preview" }));
    expect(api.importMcpServers).toHaveBeenLastCalledWith(expect.objectContaining({ sourceName: "claude_desktop_config.json" }));
    expect(within(dialog).getByText(/claude_desktop_config\.json · 3 servers/)).toBeVisible();
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
