import { useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { ArrowLeft, Copy, Download, ExternalLink, FileUp, LoaderCircle, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { McpImportEntry, McpImportReport, McpImportSecret } from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { ModalSurface } from "./DialogSystem";
import { StatusChip, type StatusTone } from "./SurfacePrimitives";
import { copySelectionText } from "./selection/selectionActions";

export const MCP_FORMAT_GUIDE_URL = "https://github.com/berylliumsec/nebula/blob/main/docs/MCP-SERVERS.md";
const MAX_FILE_BYTES = 1024 * 1024;
const PASTED_SOURCE = "Pasted configuration";

export const MCP_IMPORT_EXAMPLE = `{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "\${GITHUB_TOKEN}" }
    }
  }
}`;

/** Turn engine-specific JSON.parse messages into one line with a location. */
export function describeJsonError(text: string, error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  let location = "";
  const lineColumn = /line (\d+) column (\d+)/i.exec(message);
  const position = /position (\d+)/i.exec(message);
  if (lineColumn) {
    location = `line ${lineColumn[1]}, column ${lineColumn[2]}`;
  } else if (position) {
    const before = text.slice(0, Number(position[1]));
    location = `line ${before.split("\n").length}, column ${before.length - before.lastIndexOf("\n")}`;
  }
  const detail = message
    .replace(/^JSON(?:\.parse:| Parse error:)\s*/i, "")
    .replace(/\s+(?:in JSON )?at position \d+.*$/i, "")
    .replace(/\s+in JSON at position \d+.*$/i, "")
    .replace(/\s+(?:in object |in array )?at line \d+ column \d+.*$/i, "")
    .replace(/\s*\(line \d+ column \d+\)$/i, "")
    .trim();
  return `Not valid JSON${location ? `: ${location}` : ""}${detail ? ` — ${detail}` : ""}. Nothing was imported.`;
}

function secretSummary(secret: McpImportSecret): string {
  const label = secret.target === "Authorization bearer token"
    ? "Bearer token"
    : secret.target.replace(/^(env|header) /, "");
  if (secret.source === "environment") {
    const variable = secret.reference?.replace(/^env:/, "") ?? label;
    return variable === label ? `${label} from Nebula's environment` : `${label} from ${variable}`;
  }
  return secret.source === "vault" ? `${label} to credential vault` : `${label} kept for this session only`;
}

const rowStatus: Record<McpImportEntry["action"], [string, StatusTone]> = {
  create: ["New", "informational"],
  replace: ["Replaces existing", "warning"],
  skip: ["Exists · skipped", "warning"],
  invalid: ["Can't import", "danger"],
};

function target(entry: McpImportEntry): string | undefined {
  if (entry.url) return entry.url;
  if (entry.command) return [entry.command, ...entry.arguments].join(" ");
  return undefined;
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

interface McpImportDialogProps {
  api: ApiClient;
  onClose: () => void;
  onImported: (report: McpImportReport, sourceName: string) => void;
}

export function McpImportDialog({ api, onClose, onImported }: McpImportDialogProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [text, setText] = useState("");
  const [sourceName, setSourceName] = useState(PASTED_SOURCE);
  const [exampleOpen, setExampleOpen] = useState(true);
  const [report, setReport] = useState<McpImportReport>();
  const [replace, setReplace] = useState(false);
  const [busy, setBusy] = useState<"read" | "preview" | "import" | "schema">();
  const [parseError, setParseError] = useState<string>();
  const [error, setError] = useState<string>();
  const [copyFeedback, setCopyFeedback] = useState("");

  const updateText = (next: string) => {
    if (!text.trim() && next.trim()) setExampleOpen(false);
    setText(next);
    setParseError(undefined);
    setError(undefined);
  };

  const chooseFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (file.size > MAX_FILE_BYTES) {
      setError(`${file.name} is larger than 1 MB. MCP configuration files are usually a few kilobytes.`);
      return;
    }
    setBusy("read");
    try {
      updateText(await file.text());
      setSourceName(file.name);
    } catch (readError) {
      void logCaughtDiagnostic("interface.mcp_import.file_read_failed", "An MCP import file could not be read.", readError, "mcp_import_dialog");
      setError(`${file.name} could not be read. Paste its contents instead.`);
    } finally {
      setBusy(undefined);
    }
  };

  const parse = (): Record<string, unknown> | undefined => {
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch (jsonError) {
      // diagnostic-expected: malformed operator input is explained beside the field.
      setParseError(describeJsonError(text, jsonError));
      return undefined;
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      setParseError("The configuration must be a JSON object with an \"mcpServers\" or \"servers\" key. Nothing was imported.");
      return undefined;
    }
    return value as Record<string, unknown>;
  };

  const preview = async (nextReplace = replace) => {
    const config = parse();
    if (!config) return;
    const previous = replace;
    setReplace(nextReplace);
    setBusy("preview");
    setError(undefined);
    try {
      setReport(await api.importMcpServers({ config, dryRun: true, onConflict: nextReplace ? "replace" : "skip", sourceName }));
    } catch (previewError) {
      setReplace(previous);
      void logCaughtDiagnostic("interface.mcp_import.preview_failed", "An MCP import preview failed.", previewError, "mcp_import_dialog");
      setError(previewError instanceof Error ? previewError.message : "Nebula could not read this configuration.");
    } finally {
      setBusy(undefined);
    }
  };

  const importServers = async () => {
    const config = parse();
    if (!config) return;
    setBusy("import");
    setError(undefined);
    try {
      const applied = await api.importMcpServers({ config, dryRun: false, onConflict: replace ? "replace" : "skip", sourceName });
      if (applied.created + applied.replaced === 0) {
        setReport(applied);
        setError("No servers were saved. Review the rows below, then go back to fix the file.");
        return;
      }
      onImported(applied, sourceName);
    } catch (importError) {
      void logCaughtDiagnostic("interface.mcp_import.apply_failed", "An MCP import could not be saved.", importError, "mcp_import_dialog");
      setError(importError instanceof Error ? importError.message : "The servers could not be imported. Nothing was saved.");
    } finally {
      setBusy(undefined);
    }
  };

  const downloadSchema = async () => {
    setBusy("schema");
    setError(undefined);
    try {
      const schema = await api.mcpServerSchema();
      const url = URL.createObjectURL(new Blob([`${JSON.stringify(schema, null, 2)}\n`], { type: "application/schema+json" }));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "mcp-servers.schema.json";
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (schemaError) {
      void logCaughtDiagnostic("interface.mcp_import.schema_failed", "The MCP import schema could not be downloaded.", schemaError, "mcp_import_dialog");
      setError(schemaError instanceof Error ? schemaError.message : "The schema could not be downloaded.");
    } finally {
      setBusy(undefined);
    }
  };

  const copyExample = async () => {
    try {
      await copySelectionText(MCP_IMPORT_EXAMPLE);
      setCopyFeedback("Example copied.");
    } catch (copyError) {
      // diagnostic-expected: clipboard denial is shown beside the copy control.
      setCopyFeedback(copyError instanceof Error ? copyError.message : "Copy failed. Select the example and copy it manually.");
    }
    globalThis.setTimeout(() => setCopyFeedback(""), 2000);
  };

  const close = () => { if (!busy || busy === "schema") onClose(); };
  const submit = (event: FormEvent) => { event.preventDefault(); if (report) void importServers(); else void preview(); };

  const conflicts = report?.entries.filter((entry) => entry.action === "skip" || entry.action === "replace") ?? [];
  const saving = report ? report.created + report.replaced : 0;

  return (
    <ModalSurface as="form" className="provider-dialog resource-dialog mcp-import-dialog" labelledBy="mcp-import-title" onClose={close} onSubmit={submit}>
      <header>
        <div><small>MCP registry</small><h2 id="mcp-import-title">Import MCP servers</h2></div>
        <button className="icon-button subtle" type="button" aria-label="Close import dialog" onClick={close}><X size={17} /></button>
      </header>
      {!report ? <>
        <p className="provider-dialog-note">Paste the MCP file you use with Claude Desktop, Cursor, or VS Code, or choose it.</p>
        <div className="mcp-import-field-label">
          <label htmlFor="mcp-import-text">Configuration</label>
          <input ref={fileRef} className="sr-only" type="file" accept=".json,application/json" aria-label="Choose MCP configuration file" onChange={(event) => void chooseFile(event)} />
          <button className="button secondary" type="button" disabled={Boolean(busy)} onClick={() => fileRef.current?.click()}><FileUp size={15} /> Choose file…</button>
        </div>
        <textarea
          id="mcp-import-text"
          className="mcp-import-text"
          rows={6}
          spellCheck={false}
          value={text}
          placeholder={'{ "mcpServers": { … } }'}
          aria-invalid={parseError ? true : undefined}
          aria-describedby={parseError ? "mcp-import-parse-error" : undefined}
          onChange={(event) => updateText(event.target.value)}
        />
        {parseError && <p id="mcp-import-parse-error" className="mcp-import-error" role="alert">{parseError}</p>}
        <details className="mcp-import-example" open={exampleOpen} onToggle={(event) => setExampleOpen(event.currentTarget.open)}>
          <summary>Example</summary>
          <div className="mcp-import-example-body">
            <div className="mcp-import-example-toolbar">
              {copyFeedback && <small aria-hidden="true">{copyFeedback}</small>}
              <button className="icon-button subtle" type="button" aria-label="Copy example" title="Copy example" onClick={() => void copyExample()}><Copy size={14} /></button>
            </div>
            <pre tabIndex={0} role="region" aria-label="Example configuration"><code>{MCP_IMPORT_EXAMPLE}</code></pre>
            <p>Write secrets as <code>{"${NAME}"}</code> to read them from Nebula's environment. Typed-in secrets move to the credential vault and are never shown again.</p>
            <span className="sr-only" role="status" aria-live="polite">{copyFeedback}</span>
          </div>
        </details>
        <div className="mcp-import-links">
          <a href={MCP_FORMAT_GUIDE_URL} target="_blank" rel="noreferrer"><ExternalLink size={13} aria-hidden="true" /> Format guide</a>
          <button type="button" disabled={busy === "schema"} onClick={() => void downloadSchema()}><Download size={13} aria-hidden="true" /> Download schema</button>
        </div>
      </> : <>
        <div className="mcp-import-summary">
          <span>{sourceName} · {plural(report.entries.length, "server")}</span>
          {report.created > 0 && <StatusChip tone="informational">{report.created} new</StatusChip>}
          {report.replaced > 0 && <StatusChip tone="warning">{report.replaced} replace{report.replaced === 1 ? "s" : ""}</StatusChip>}
          {report.skipped > 0 && <StatusChip tone="warning">{report.skipped} exist{report.skipped === 1 ? "s" : ""}</StatusChip>}
          {report.invalid > 0 && <StatusChip tone="danger">{report.invalid} can't import</StatusChip>}
        </div>
        <ul className="mcp-import-rows" aria-label="Servers in this file">
          {report.entries.map((entry) => {
            const [status, tone] = rowStatus[entry.action];
            const where = target(entry);
            return <li className="mcp-import-row" key={entry.sourceName}>
              <div className="mcp-import-row-top">
                <strong>{entry.name ?? entry.sourceName}</strong>
                {entry.transport && <span>{entry.transport === "stdio" ? "stdio" : "HTTP"}</span>}
                <StatusChip tone={tone}>{status}</StatusChip>
              </div>
              {where && <code>{where}</code>}
              {entry.error && <p className="mcp-import-error">{entry.error}</p>}
              {entry.secrets.length > 0 && <p className="mcp-import-secrets">{entry.secrets.map(secretSummary).join(" · ")}</p>}
              {entry.warnings.length > 0 && <details>
                <summary>{plural(entry.warnings.length, "note")}</summary>
                <ul>{entry.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
              </details>}
            </li>;
          })}
        </ul>
        {conflicts.length > 0 && <label className="provider-consent">
          <input type="checkbox" checked={replace} disabled={Boolean(busy)} onChange={(event) => void preview(event.target.checked)} />
          <span>
            <strong>{conflicts.length === 1 ? `Replace ${conflicts[0].name ?? conflicts[0].sourceName}` : `Replace ${conflicts.length} existing servers`}</strong>
            <small>Overwrites the existing server with this file's settings. It is disabled and untrusted again until you review it.</small>
          </span>
        </label>}
      </>}
      {error && <DiagnosticErrorNotice error={error} fallback="The import could not be completed." compact />}
      <footer>
        {report
          ? <>
            <p className="mcp-import-footnote">Imported servers start disabled. Trust, probe, and enable each one afterwards.</p>
            <button className="button secondary" type="button" disabled={Boolean(busy)} onClick={() => { setReport(undefined); setReplace(false); setError(undefined); }}><ArrowLeft size={15} /> Back</button>
            <button className="button primary" type="submit" disabled={Boolean(busy) || saving === 0}>
              {busy === "import" ? <><LoaderCircle className="spin" size={15} /> Importing…</> : busy === "preview" ? <><LoaderCircle className="spin" size={15} /> Checking…</> : `Import ${plural(saving, "server")}`}
            </button>
          </>
          : <>
            <button className="button secondary" type="button" disabled={Boolean(busy) && busy !== "schema"} onClick={close}>Cancel</button>
            <button className="button primary" type="submit" disabled={Boolean(busy) || !text.trim() || Boolean(parseError)}>
              {busy === "preview" ? <><LoaderCircle className="spin" size={15} /> Checking…</> : "Preview"}
            </button>
          </>}
      </footer>
    </ModalSurface>
  );
}
