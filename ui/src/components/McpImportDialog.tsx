import { useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { ArrowLeft, Copy, Download, ExternalLink, FileUp, LoaderCircle, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { McpImportEntry, McpImportReport, McpImportSecret, McpServerProfile } from "../api/types";
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
  update: ["Update", "warning"],
  unchanged: ["Unchanged", "neutral"],
  replace: ["Replaces existing", "warning"],
  skip: ["Exists · skipped", "warning"],
  invalid: ["Can't import", "danger"],
};

type Approval = McpServerProfile["defaultApproval"];

export const MCP_APPROVAL_CHOICES: { value: Approval; label: string; help: string }[] = [
  { value: "risk_based", label: "Risk-based", help: "Asks unless a probed tool is read-only, non-destructive, and uses no credentials." },
  { value: "ask", label: "Ask", help: "Asks before every tool call." },
  { value: "allow", label: "Allow", help: "Runs tools without asking. Tools marked destructive still ask." },
  { value: "deny", label: "Deny", help: "Blocks every tool call from these servers." },
];

function approvalLabel(value?: Approval): string {
  return MCP_APPROVAL_CHOICES.find((choice) => choice.value === value)?.label ?? "Risk-based";
}

/** What the server will look like once saved, for rows that save something. */
function outcome(entry: McpImportEntry): string | undefined {
  const approval = approvalLabel(entry.defaultApproval);
  const fresh = entry.action === "create" || entry.action === "replace";
  if (!fresh && entry.action !== "update") return undefined;
  if (entry.enabled) return `${fresh ? "Enabled" : "Stays enabled"} · ${approval}`;
  if (entry.needsTrust) return "Disabled until trusted";
  return `${fresh ? "Disabled" : "Stays disabled"} · ${approval}`;
}

function listNames(names: string[]): string {
  return names.length <= 2 ? names.join(" and ") : `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
}

interface ImportChoices {
  enabled: boolean;
  approval: Approval;
  trust: boolean;
}

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
  const [choices, setChoices] = useState<ImportChoices>({ enabled: false, approval: "risk_based", trust: false });
  const previewRequest = useRef(0);
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

  const request = (config: Record<string, unknown>, dryRun: boolean, next: ImportChoices) => api.importMcpServers({
    config,
    dryRun,
    onConflict: "update",
    sourceName,
    defaults: { enabled: next.enabled, defaultApproval: next.approval },
    trustLocalPrograms: next.trust,
  });

  /** Core's dry run is the source of truth for every row, so each choice previews again. */
  const preview = async (change: Partial<ImportChoices> = {}) => {
    const config = parse();
    if (!config) return;
    const previous = choices;
    const next = { ...choices, ...change };
    setChoices(next);
    const current = ++previewRequest.current;
    setBusy("preview");
    setError(undefined);
    try {
      const result = await request(config, true, next);
      if (current !== previewRequest.current) return;
      if (next.trust && !result.entries.some((entry) => entry.needsTrust)) setChoices({ ...next, trust: false });
      setReport(result);
    } catch (previewError) {
      if (current !== previewRequest.current) return;
      setChoices(previous);
      void logCaughtDiagnostic("interface.mcp_import.preview_failed", "An MCP import preview failed.", previewError, "mcp_import_dialog");
      setError(previewError instanceof Error ? previewError.message : "Nebula could not read this configuration.");
    } finally {
      if (current === previewRequest.current) setBusy(undefined);
    }
  };

  const importServers = async () => {
    const config = parse();
    if (!config) return;
    setBusy("import");
    setError(undefined);
    try {
      const applied = await request(config, false, choices);
      if (applied.created + applied.replaced + applied.updated === 0) {
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

  const saving = report ? report.created + report.replaced + report.updated : 0;
  const hasNew = report?.entries.some((entry) => entry.action === "create" || entry.action === "replace") ?? false;
  const trustTargets = report?.entries.filter((entry) => entry.needsTrust) ?? [];
  const trustNames = trustTargets.map((entry) => entry.name ?? entry.sourceName);
  const relaunched = trustTargets.length === 1 && trustTargets[0].action === "update";
  const approvalHelp = MCP_APPROVAL_CHOICES.find((choice) => choice.value === choices.approval)?.help ?? "";
  const footnote = report && report.updated + report.unchanged > 0
    ? "Only what changed is saved. Nebula settings the file doesn't mention are kept."
    : choices.enabled
      ? "Nebula probes enabled servers right after saving."
      : "New servers start disabled. Trust, probe, and enable each one afterwards.";

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
          {report.updated > 0 && <StatusChip tone="warning">{report.updated} update{report.updated === 1 ? "" : "s"}</StatusChip>}
          {report.unchanged > 0 && <StatusChip tone="neutral">{report.unchanged} unchanged</StatusChip>}
          {report.replaced > 0 && <StatusChip tone="warning">{report.replaced} replace{report.replaced === 1 ? "s" : ""}</StatusChip>}
          {report.skipped > 0 && <StatusChip tone="warning">{report.skipped} exist{report.skipped === 1 ? "s" : ""}</StatusChip>}
          {report.invalid > 0 && <StatusChip tone="danger">{report.invalid} can't import</StatusChip>}
        </div>
        <ul className="mcp-import-rows" aria-label="Servers in this file">
          {report.entries.map((entry) => {
            const [status, tone] = rowStatus[entry.action];
            const where = target(entry);
            const result = outcome(entry);
            const name = entry.name ?? entry.sourceName;
            return <li className="mcp-import-row" key={entry.sourceName}>
              <div className="mcp-import-row-top">
                <strong>{name}</strong>
                {entry.transport && <span>{entry.transport === "stdio" ? "stdio" : "HTTP"}</span>}
                {result && <span className="mcp-import-outcome">{result}</span>}
                <StatusChip tone={tone}>{status}</StatusChip>
              </div>
              {where && <code>{where}</code>}
              {entry.error && <p className="mcp-import-error">{entry.error}</p>}
              {entry.action === "unchanged"
                ? <p className="mcp-import-secrets">Matches the saved server. Nothing to save.</p>
                : entry.secrets.length > 0 && <p className="mcp-import-secrets">{entry.secrets.map(secretSummary).join(" · ")}</p>}
              {entry.changes.length > 0 && <details className="mcp-import-changes" open>
                <summary>{plural(entry.changes.length, "change")}</summary>
                <ul>{entry.changes.map((change) => <li key={change.field}>
                  <span>{change.field}</span> <code>{change.before ?? "not set"}</code> <span aria-hidden="true">→</span><span className="sr-only">becomes</span> <code>{change.after ?? "removed"}</code>
                </li>)}</ul>
              </details>}
              {entry.action === "update" && entry.needsTrust && !entry.enabled && <p className="mcp-import-trust-note">Its launch settings changed, so {name} is untrusted again. Tick Trust below to keep it enabled.</p>}
              {entry.warnings.length > 0 && <details>
                <summary>{plural(entry.warnings.length, "note")}</summary>
                <ul>{entry.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
              </details>}
            </li>;
          })}
        </ul>
        {hasNew && <fieldset className="mcp-import-choices">
          <legend>New servers</legend>
          <label className="provider-consent">
            <input type="checkbox" checked={choices.enabled} onChange={(event) => void preview({ enabled: event.target.checked, trust: false })} />
            <span>
              <strong>Enable after import</strong>
              <small>Nebula probes each new server right away so its tools are ready to use.</small>
            </span>
          </label>
          <fieldset className="mcp-import-approval" aria-describedby="mcp-import-approval-help">
            <legend>Tool approval</legend>
            <div className="mcp-import-approval-options">
              {MCP_APPROVAL_CHOICES.map((choice) => <label key={choice.value}>
                <input type="radio" name="mcp-import-approval" value={choice.value} checked={choices.approval === choice.value} onChange={() => void preview({ approval: choice.value })} />
                <span>{choice.label}</span>
              </label>)}
            </div>
            <small id="mcp-import-approval-help">{approvalHelp} A server whose file sets nebula.default_approval keeps its own.</small>
          </fieldset>
        </fieldset>}
        {trustTargets.length > 0 && <label className="provider-consent">
          <input type="checkbox" checked={choices.trust} onChange={(event) => void preview({ trust: event.target.checked })} />
          <span>
            <strong>{trustNames.length === 1 ? `Trust ${trustNames[0]} to run on this Core` : `Trust ${trustNames.length} local programs to run on this Core`}</strong>
            <small>{relaunched ? "Its launch settings changed. " : ""}Local programs run on the Core host, outside the automation container. Without this, {listNames(trustNames)} {trustNames.length === 1 ? "is" : "are"} saved disabled.</small>
          </span>
        </label>}
      </>}
      {error && <DiagnosticErrorNotice error={error} fallback="The import could not be completed." compact />}
      {/* Keys stop React from turning the clicked Back button into Preview mid-click, which would submit the form. */}
      <footer>
        {report
          ? <>
            <p className="mcp-import-footnote">{footnote}</p>
            <button key="back" className="button secondary" type="button" disabled={Boolean(busy)} onClick={() => { setReport(undefined); setError(undefined); }}><ArrowLeft size={15} /> Back</button>
            <button key="save" className="button primary" type="submit" disabled={Boolean(busy) || saving === 0}>
              {busy === "import" ? <><LoaderCircle className="spin" size={15} /> Saving…</> : busy === "preview" ? <><LoaderCircle className="spin" size={15} /> Checking…</> : saving ? `Save ${plural(saving, "server")}` : "Nothing to save"}
            </button>
          </>
          : <>
            <button key="cancel" className="button secondary" type="button" disabled={Boolean(busy) && busy !== "schema"} onClick={close}>Cancel</button>
            <button key="preview" className="button primary" type="submit" disabled={Boolean(busy) || !text.trim() || Boolean(parseError)}>
              {busy === "preview" ? <><LoaderCircle className="spin" size={15} /> Checking…</> : "Preview"}
            </button>
          </>}
      </footer>
    </ModalSurface>
  );
}
