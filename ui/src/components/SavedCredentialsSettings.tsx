import { useCallback, useEffect, useState, type FormEvent } from "react";
import { KeyRound, Plus } from "lucide-react";
import type { McpServerProfile, SavedCredential } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { announceSettingsSaved } from "./SettingsSaveFeedback";
import { useConfirmation } from "./DialogSystem";

const NAME_PATTERN = /^[a-z0-9][a-z0-9-]{0,63}$/;

type Draft = { name: string; description: string; secret: string; servers: string[] };
const EMPTY: Draft = { name: "", description: "", secret: "", servers: [] };

/** Turns a label such as "Mac login" into a placeholder name such as "mac-login". */
export function credentialName(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+/, "").replace(/-{2,}/g, "-").slice(0, 64);
}

export function SavedCredentialsSettings() {
  const confirm = useConfirmation();
  const { api, coreState, previewMode } = useWorkspace();
  const [items, setItems] = useState<SavedCredential[]>([]);
  const [servers, setServers] = useState<McpServerProfile[]>([]);
  const [editing, setEditing] = useState<SavedCredential | "new">();
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();

  const load = useCallback(async () => {
    if (!api || coreState !== "online") return;
    try {
      const [saved, profiles] = await Promise.all([api.listSavedCredentials(), api.listMcpServers()]);
      setItems(saved);
      setServers(profiles);
      setError(undefined);
    } catch (loadError) {
      void logCaughtDiagnostic("interface.saved_credentials.load_failed", "Saved passwords could not be loaded.", loadError, "credentials");
      setError(loadError);
    }
  }, [api, coreState]);

  useEffect(() => { void load(); }, [load]);

  // Built-in servers (notebooks, security tools) cannot type into a prompt.
  // Servers already allowed stay listed, and first, so they can be unticked.
  // Order by what was allowed when the form opened, so rows never jump mid-edit.
  const opened = editing && editing !== "new" ? editing.allowedMcpServerIds : [];
  const choices = servers
    .filter((server) => server.transport !== "builtin" || opened.includes(server.id))
    .sort((a, b) => Number(opened.includes(b.id)) - Number(opened.includes(a.id)) || a.name.localeCompare(b.name));
  const serverName = (id: string) => servers.find((server) => server.id === id)?.name ?? "Removed server";
  const disabled = previewMode || !api || coreState !== "online" || busy;
  const isNew = editing === "new";
  const nameValid = !isNew || NAME_PATTERN.test(draft.name);
  const canSave = Boolean(editing) && nameValid && (!isNew || Boolean(draft.secret));

  const open = (target: SavedCredential | "new") => {
    setEditing(target);
    setDraft(target === "new" ? EMPTY : { name: target.name, description: target.description, secret: "", servers: target.allowedMcpServerIds });
  };
  const close = () => { setEditing(undefined); setDraft(EMPTY); };

  const toggleServer = (id: string) => setDraft((current) => ({
    ...current,
    servers: current.servers.includes(id) ? current.servers.filter((item) => item !== id) : [...current.servers, id],
  }));

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !editing || !canSave) return;
    setBusy(true);
    setError(undefined);
    try {
      if (editing === "new") {
        await api.createSavedCredential({ name: draft.name, description: draft.description, secret: draft.secret, allowedMcpServerIds: draft.servers });
      } else {
        await api.updateSavedCredential(editing.name, {
          description: draft.description,
          allowedMcpServerIds: draft.servers,
          ...(draft.secret ? { secret: draft.secret } : {}),
          expectedRevision: editing.revision,
        });
      }
      // Report saved only after Core's list shows the change.
      const saved = await api.listSavedCredentials();
      setItems(saved);
      if (saved.some((item) => item.name === draft.name && item.available)) announceSettingsSaved("Password saved");
      close();
    } catch (saveError) {
      void logCaughtDiagnostic("interface.saved_credentials.save_failed", "The saved password could not be saved.", saveError, "credentials");
      setError(saveError);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (item: SavedCredential) => {
    if (!api) return;
    const approved = await confirm({
      title: `Delete ${item.name}?`,
      message: <>Agents can no longer enter this password. Nebula deletes its encrypted copy.</>,
      confirmLabel: "Delete password",
      tone: "danger",
    });
    if (!approved) return;
    setBusy(true);
    setError(undefined);
    try {
      await api.deleteSavedCredential(item.name, item.revision);
      const saved = await api.listSavedCredentials();
      setItems(saved);
      if (!saved.some((entry) => entry.name === item.name)) announceSettingsSaved("Password deleted");
    } catch (removeError) {
      void logCaughtDiagnostic("interface.saved_credentials.delete_failed", "The saved password could not be deleted.", removeError, "credentials");
      setError(removeError);
    } finally {
      setBusy(false);
    }
  };

  return <section className="settings-section" id="saved-credential-settings">
    <div className="section-heading">
      <div><h2>Saved passwords</h2><p>Passwords agents can type into prompts through an MCP server you allow. Core keeps them encrypted and models never see them.</p></div>
      <button className="button primary" type="button" disabled={disabled || Boolean(editing)} onClick={() => open("new")}><Plus size={16} /> Add password</button>
    </div>
    {Boolean(error) && <DiagnosticErrorNotice error={error} fallback="The saved password operation failed." compact />}
    <div className="panel typesafe-panel saved-credentials-panel">
      {editing && <form className="typesafe-form" onSubmit={(event) => void save(event)}>
        <label>Name
          <input value={draft.name} disabled={disabled || !isNew} placeholder="mac-login" autoComplete="off" spellCheck={false}
            aria-invalid={!nameValid} onChange={(event) => setDraft({ ...draft, name: credentialName(event.target.value) })} />
          <small className="saved-credential-hint">{nameValid ? <>Agents write <code>{`{{credential:${draft.name || "name"}}}`}</code> where the password goes.</> : "Use lowercase letters, numbers and dashes."}</small>
        </label>
        <label>What it is for<input value={draft.description} maxLength={500} disabled={disabled} placeholder="macOS login and sudo dialogs" onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></label>
        <label>{isNew ? "Password" : "New password"}
          <input type="password" autoComplete="new-password" value={draft.secret} disabled={disabled} placeholder={isNew ? "Password" : "Leave blank to keep the current password"} onChange={(event) => setDraft({ ...draft, secret: event.target.value })} />
        </label>
        <fieldset className="saved-credential-servers" disabled={disabled}>
          <legend>MCP servers allowed to receive it</legend>
          {choices.length === 0 && <p className="provider-dialog-note">Add an MCP server first, such as one that controls your Mac. Until one is allowed, no agent can use this password.</p>}
          {choices.map((server) => <label key={server.id} className="provider-consent">
            <input type="checkbox" checked={draft.servers.includes(server.id)} onChange={() => toggleServer(server.id)} />
            <span><strong>{server.name}</strong>{server.description && <small>{server.description}</small>}</span>
          </label>)}
        </fieldset>
        <footer>
          <span>Agents can enter it only through the servers you tick.</span>
          <span className="typesafe-actions">
            <button className="button secondary" type="button" disabled={busy} onClick={close}>Cancel</button>
            <button className="button primary" type="submit" disabled={disabled || !canSave}>{busy ? "Saving…" : "Save password"}</button>
          </span>
        </footer>
      </form>}
      {!editing && items.length === 0 && <p className="saved-credential-empty">No saved passwords yet. Add one, then allow the MCP server that types into your Mac's dialogs.</p>}
      {!editing && items.length > 0 && <ul className="saved-credential-list">
        {items.map((item) => <li key={item.name}>
          <KeyRound size={16} aria-hidden="true" />
          <div>
            <strong>{item.name}</strong>
            {item.description && <p>{item.description}</p>}
            <p><code>{item.placeholder}</code> · {item.allowedMcpServerIds.length ? item.allowedMcpServerIds.map(serverName).join(", ") : "No server allowed yet"}</p>
          </div>
          {!item.available && <span className="integration-status bad" role="status"><span aria-hidden="true" />Unavailable</span>}
          <span className="typesafe-actions">
            <button className="button secondary" type="button" disabled={disabled} onClick={() => open(item)}>Edit</button>
            <button className="button danger-quiet" type="button" disabled={disabled} onClick={() => void remove(item)}>Delete</button>
          </span>
        </li>)}
      </ul>}
    </div>
  </section>;
}
