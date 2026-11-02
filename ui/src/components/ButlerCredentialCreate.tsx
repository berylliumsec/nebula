import {useEffect, useState, type FormEvent} from "react";
import type {ApiClient} from "../api/client";
import {logCaughtDiagnostic} from "../diagnostics";
import styles from "./NebulaControlAssistant.module.css";

interface Props {
  api: Pick<ApiClient, "credentialVaultStatus" | "createCredential" | "credentialStatus">;
  projectId: string;
  currentProjectId: () => string | undefined;
  onSaved: (reference: string, persistence: "vault" | "session") => void;
  onUncertain: (reference: string) => void;
  onCancel: () => void;
}

/** Only this browser sees the secret; Butler receives the new reference and status. */
export function ButlerCredentialCreate({api, projectId, currentProjectId, onSaved, onUncertain, onCancel}: Props) {
  const [secret, setSecret] = useState("");
  const [persistence, setPersistence] = useState<"vault" | "session">("session");
  const [vaultAvailable, setVaultAvailable] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void api.credentialVaultStatus().then((status) => {
      if (active) setVaultAvailable(status.available);
    }).catch((caught) => {
      logCaughtDiagnostic("interface.butler_credential.vault_status_failed", "Credential vault status could not be read.", caught, "credentials");
    });
    return () => { active = false; };
  }, [api]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || !secret || currentProjectId() !== projectId) return;
    setBusy(true);
    setError("");
    let issuedReference: string | undefined;
    try {
      const created = await api.createCredential(secret, persistence);
      issuedReference = created.reference;
      setSecret("");
      if (currentProjectId() !== projectId) {
        onUncertain(issuedReference);
        return;
      }
      const observed = await api.credentialStatus(issuedReference);
      if (observed.reference !== issuedReference || observed.persistence !== persistence || !observed.available || observed.state !== "available") {
        onUncertain(issuedReference);
        return;
      }
      onSaved(issuedReference, persistence);
    } catch (caught) {
      logCaughtDiagnostic("interface.butler_credential.create_failed", "Credential creation or readback failed.", caught, "credentials");
      if (issuedReference) onUncertain(issuedReference);
      else setError(caught instanceof Error ? caught.message : "Core could not save the credential. Retry after checking the vault.");
    } finally {
      setBusy(false);
    }
  };

  return <form className={styles.credentialForm} aria-label="Add a write-only credential" onSubmit={(event) => void submit(event)}>
    <strong>Add a write-only credential</strong>
    <small>Enter the secret on this device. Butler will receive only a reference; attach it to the intended provider or integration separately.</small>
    <label>Secret<input type="password" autoComplete="new-password" value={secret} maxLength={16384} required disabled={busy} onChange={(event) => setSecret(event.target.value)} /></label>
    <label>Storage<select value={persistence} disabled={busy} onChange={(event) => setPersistence(event.target.value as "vault" | "session")}>
      <option value="session">This Core session only</option>
      <option value="vault" disabled={!vaultAvailable}>Operating-system vault</option>
    </select></label>
    {!vaultAvailable && <small>The operating-system vault is unavailable or locked. Session storage is available.</small>}
    {error && <p role="alert">{error}</p>}
    <div><button className="button quiet" type="button" disabled={busy} onClick={onCancel}>Cancel</button>
      <button className="button primary" type="submit" disabled={busy || !secret || currentProjectId() !== projectId}>{busy ? "Saving…" : "Save credential"}</button></div>
  </form>;
}
