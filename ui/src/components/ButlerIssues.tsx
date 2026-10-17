import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { HarnessProfile, McpServerProfile } from "../api/types";
import styles from "./ButlerOversight.module.css";

type Destination = {
  id: string; revision: number; name: string; repository: string;
  publisher: "github" | "mcp"; credential_ref?: string; mcp_profile_id?: string;
  review_profile_id: string; auto_publish: boolean; enabled: boolean;
};
type Candidate = {
  id: string; revision: number; destination_id: string; title: string; body: string;
  reproduction: string; confidence: string; public_independent: boolean;
  status: string; reason: string; issue_url?: string; source_kind: string;
};
type Scan = { id: string; source_kind: string; reason: string };
type Result = { destinations: Destination[]; candidates: Candidate[]; held_scans: Scan[] };
type Draft = Pick<Destination, "name" | "repository" | "publisher" | "review_profile_id" | "auto_publish" | "enabled"> & {
  credential_ref?: string; mcp_profile_id?: string;
};
const blank: Draft = { name: "", repository: "", publisher: "github", review_profile_id: "", auto_publish: false, enabled: true };

function endpoint(projectId: string): string {
  return `engagements/${encodeURIComponent(projectId)}/butler/issues`;
}

export function ButlerIssues({ api, projectId, online }: { api?: ApiClient; projectId: string; online: boolean }) {
  const [result, setResult] = useState<Result>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<Destination>();
  const [draft, setDraft] = useState<Draft>(blank);
  const [formOpen, setFormOpen] = useState(false);
  const [harnesses, setHarnesses] = useState<HarnessProfile[]>([]);
  const [servers, setServers] = useState<McpServerProfile[]>([]);
  const [secret, setSecret] = useState("");
  const [repositories, setRepositories] = useState<Array<{ repository: string; visibility: string }>>([]);
  const [repoPage, setRepoPage] = useState(1);
  const [hasMoreRepos, setHasMoreRepos] = useState(false);
  const [revising, setRevising] = useState<Candidate>();
  const [revision, setRevision] = useState({ title: "", body: "", reproduction: "", public_independent: false });

  const refresh = async (signal?: AbortSignal) => {
    if (!api || !online) return;
    try {
      const next = await api.request<Result>(endpoint(projectId), { signal });
      if (signal?.aborted) return;
      setResult({ destinations: next.destinations ?? [], candidates: next.candidates ?? [], held_scans: next.held_scans ?? [] });
      setError("");
    } catch (caught) {
      if (!signal?.aborted) setError(caught instanceof Error ? caught.message : "Could not load improvement issues.");
    }
  };

  useEffect(() => {
    setResult(undefined); setEditing(undefined); setDraft(blank); setFormOpen(false); setRepositories([]);
    if (!api || !online) return;
    const controller = new AbortController();
    void refresh(controller.signal);
    const timer = window.setInterval(() => void refresh(controller.signal), 30000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [api, projectId, online]);

  useEffect(() => {
    if (!formOpen || !api || !online) return;
    let active = true;
    void Promise.all([api.listHarnesses(), api.listMcpServers()]).then(([nextHarnesses, nextServers]) => {
      if (active) { setHarnesses(nextHarnesses); setServers(nextServers); }
    }).catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : "Could not load runtimes."); });
    return () => { active = false; };
  }, [formOpen, api, online]);

  const loadRepositories = async (ref: string, page = 1) => {
    if (!api || !ref) return;
    setBusy(true);
    try {
      const next = await api.request<{ repositories: Array<{ repository: string; visibility: string }>; has_more: boolean }>(
        `${endpoint(projectId)}/github-repositories?credential_ref=${encodeURIComponent(ref)}&page=${page}`,
      );
      setRepositories(page === 1 ? next.repositories : [...repositories, ...next.repositories]);
      setHasMoreRepos(next.has_more); setRepoPage(page); setError("");
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not list repositories."); }
    finally { setBusy(false); }
  };

  const saveCredential = async () => {
    if (!api || !secret.trim()) return;
    setBusy(true);
    try {
      const vault = await api.credentialVaultStatus();
      const saved = await api.createCredential(secret.trim(), vault.available ? "vault" : "session");
      setDraft((current) => ({ ...current, credential_ref: saved.reference }));
      setSecret("");
      await loadRepositories(saved.reference);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not save GitHub credential."); }
    finally { setBusy(false); }
  };

  const saveDestination = async () => {
    if (!api || busy) return;
    setBusy(true);
    try {
      const path = `${endpoint(projectId)}/destinations${editing ? `/${encodeURIComponent(editing.id)}?expected_revision=${editing.revision}` : ""}`;
      await api.request(path, { method: editing ? "PUT" : "POST", body: JSON.stringify({
        ...draft, credential_ref: draft.publisher === "github" ? draft.credential_ref : null,
        mcp_profile_id: draft.publisher === "mcp" ? draft.mcp_profile_id : null,
      }) });
      setFormOpen(false); setEditing(undefined); setDraft(blank); setRepositories([]); setError("");
      await refresh();
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not save destination."); }
    finally { setBusy(false); }
  };

  const saveRevision = async () => {
    if (!api || !revising || busy) return;
    setBusy(true);
    try {
      await api.request(`${endpoint(projectId)}/candidates/${encodeURIComponent(revising.id)}`, {
        method: "PATCH", body: JSON.stringify({ ...revision, expected_revision: revising.revision }),
      });
      setRevising(undefined); setError(""); await refresh();
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not revise draft."); }
    finally { setBusy(false); }
  };

  const held = (result?.candidates ?? []).filter((item) => item.status === "held" || item.status === "uncertain");
  const published = (result?.candidates ?? []).filter((item) => item.status === "published");
  const pending = (result?.candidates ?? []).filter((item) => !held.includes(item) && !published.includes(item));
  return <div className={styles.section} aria-label="Improvement issues">
    <h3>Improvement issues <span>{held.length} held · {published.length} published</span></h3>
    <p className={styles.note}>Butler reviews completed Nebula chats with host Codex. Each destination controls where safe drafts may publish.</p>
    {error && <div className={styles.error} role="alert">{error}<button className="button quiet" type="button" onClick={() => void refresh()} disabled={!online}>Retry</button></div>}
    {!result && !error && <p role="status" className={styles.note}>Loading issue destinations…</p>}
    {result && <>
      {result.destinations.length ? <ul className={styles.list}>{result.destinations.map((item) => <li className={styles.row} key={item.id}>
        <div className={styles.rowHead}><strong>{item.name}</strong><span>{item.repository} · {item.publisher === "github" ? "GitHub" : "MCP"}</span></div>
        <small>{item.enabled ? item.auto_publish ? "Safe drafts publish automatically" : "Automatic publishing off" : "Disabled"}</small>
        <button className="button quiet" type="button" disabled={!online} onClick={() => {
          setEditing(item); setDraft(item); setFormOpen(true); setRepositories([]);
        }}>Edit destination</button>
      </li>)}</ul> : <p className={styles.note}>No issue destinations configured.</p>}
      {!formOpen && <button className="button quiet" type="button" disabled={!online} onClick={() => {
        setEditing(undefined); setDraft(blank); setFormOpen(true); setError("");
      }}>Add issue destination</button>}
      {formOpen && <div className={styles.row}>
        <strong>{editing ? "Edit issue destination" : "Add issue destination"}</strong>
        <label className={styles.settingsLabel}>Name<input className={styles.authoritySelect} value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
        <label className={styles.settingsLabel}>Publisher<select className={styles.authoritySelect} disabled={Boolean(editing)} value={draft.publisher} onChange={(event) => setDraft({ ...draft, publisher: event.target.value as Draft["publisher"], repository: "" })}>
          <option value="github">GitHub Issues</option><option value="mcp">Trusted MCP issue tool</option>
        </select></label>
        <label className={styles.settingsLabel}>Host Codex reviewer<select className={styles.authoritySelect} value={draft.review_profile_id} onChange={(event) => setDraft({ ...draft, review_profile_id: event.target.value })}>
          <option value="">Choose Codex</option>{harnesses.filter((item) => item.enabled && item.kind === "codex_app_server" && item.defaultModel).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>
        {draft.publisher === "github" ? <>
          <label className={styles.settingsLabel}>GitHub token<input className={styles.authoritySelect} type="password" autoComplete="off" value={secret} onChange={(event) => setSecret(event.target.value)} /></label>
          <button className="button quiet" type="button" disabled={!secret.trim() || busy} onClick={() => void saveCredential()}>Save token and browse repositories</button>
          {draft.credential_ref && <label className={styles.settingsLabel}>Repository<select className={styles.authoritySelect} disabled={Boolean(editing)} value={draft.repository} onChange={(event) => setDraft({ ...draft, repository: event.target.value })}>
            <option value="">Choose repository</option>{draft.repository && !repositories.some((item) => item.repository === draft.repository) && <option value={draft.repository}>{draft.repository}</option>}
            {repositories.map((item) => <option key={item.repository} value={item.repository}>{item.repository} · {item.visibility}</option>)}
          </select></label>}
          {!editing && hasMoreRepos && <button className="button quiet" type="button" disabled={busy} onClick={() => void loadRepositories(draft.credential_ref!, repoPage + 1)}>More repositories</button>}
        </> : <>
          <label className={styles.settingsLabel}>Trusted MCP server<select className={styles.authoritySelect} disabled={Boolean(editing)} value={draft.mcp_profile_id ?? ""} onChange={(event) => setDraft({ ...draft, mcp_profile_id: event.target.value })}>
            <option value="">Choose issue tool</option>{servers.filter((item) => item.enabled && (item.transport !== "stdio" || item.trustedStdio) && ["issue_target", "issue_find", "issue_create"].every((name) => item.tools.some((tool) => tool.name === name))).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select></label>
          <label className={styles.settingsLabel}>Repository owner/name<input className={styles.authoritySelect} readOnly={Boolean(editing)} value={draft.repository} onChange={(event) => setDraft({ ...draft, repository: event.target.value })} /></label>
        </>}
        {editing && <p className={styles.note}>Create another destination to use a different repository or issue tool.</p>}
        <label className={styles.grantLabel}><input type="checkbox" checked={draft.auto_publish} onChange={(event) => setDraft({ ...draft, auto_publish: event.target.checked })} /><span>Automatically publish drafts that pass Core's destination and disclosure checks</span></label>
        <label className={styles.grantLabel}><input type="checkbox" checked={draft.enabled} onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} /><span>Enabled</span></label>
        <div className={styles.actions}><button className="button" type="button" disabled={busy || !draft.name || !draft.repository || !draft.review_profile_id || (draft.publisher === "github" ? !draft.credential_ref : !draft.mcp_profile_id)} onClick={() => void saveDestination()}>{busy ? "Saving…" : "Save destination"}</button>
          <button className="button quiet" type="button" onClick={() => { setFormOpen(false); setSecret(""); }}>Cancel</button></div>
      </div>}
      {held.length > 0 && <ul className={styles.list} aria-label="Held improvement drafts">{held.map((item) => <li className={`${styles.row} ${styles.actionUncertain}`} key={item.id}>
        <div className={styles.rowHead}><strong>{item.title}</strong><span>{item.status}</span></div>
        <small>{result.destinations.find((destination) => destination.id === item.destination_id)?.repository} · {item.confidence}</small>
        <p className={styles.actionWarning}>{item.reason.replaceAll("_", " ") || "Review required"}</p>
        <details className={styles.evidence}><summary>Review draft</summary><p>{item.body}</p><p><strong>Reproduction:</strong> {item.reproduction}</p></details>
        <button className="button quiet" type="button" disabled={!online || item.status === "uncertain"} onClick={() => {
          setRevising(item); setRevision({ title: item.title, body: item.body, reproduction: item.reproduction, public_independent: item.public_independent });
        }}>Revise draft</button>
      </li>)}</ul>}
      {revising && <div className={styles.row}>
        <strong>Revise held draft</strong>
        <label className={styles.settingsLabel}>Title<input className={styles.authoritySelect} value={revision.title} onChange={(event) => setRevision({ ...revision, title: event.target.value })} /></label>
        <label className={styles.settingsLabel}>Issue body<textarea className={styles.authoritySelect} rows={6} value={revision.body} onChange={(event) => setRevision({ ...revision, body: event.target.value })} /></label>
        <label className={styles.settingsLabel}>Reproduction<textarea className={styles.authoritySelect} rows={3} value={revision.reproduction} onChange={(event) => setRevision({ ...revision, reproduction: event.target.value })} /></label>
        <label className={styles.grantLabel}><input type="checkbox" checked={revision.public_independent} onChange={(event) => setRevision({ ...revision, public_independent: event.target.checked })} /><span>This reproduction stands without private research</span></label>
        <div className={styles.actions}><button className="button" type="button" disabled={busy} onClick={() => void saveRevision()}>Save revision</button><button className="button quiet" type="button" onClick={() => setRevising(undefined)}>Cancel</button></div>
      </div>}
      {result.held_scans.length > 0 && <p role="alert" className={styles.actionWarning}>{result.held_scans.length} source review{result.held_scans.length === 1 ? "" : "s"} need attention. No issue was published from those reviews.</p>}
      {pending.length > 0 && <p className={styles.note}>{pending.length} issue draft{pending.length === 1 ? "" : "s"} awaiting publication check.</p>}
      {published.length > 0 && <details className={styles.disclosure}><summary>Published issues <span>{published.length}</span></summary><ul className={styles.list}>{published.map((item) => <li className={styles.row} key={item.id}><a href={item.issue_url} target="_blank" rel="noreferrer">{item.title}</a><small>{result.destinations.find((destination) => destination.id === item.destination_id)?.repository}</small></li>)}</ul></details>}
      <button className={styles.refresh} type="button" aria-label="Refresh improvement issues" title="Refresh improvement issues" disabled={!online} onClick={() => void refresh()}><RefreshCw size={17} aria-hidden="true" /></button>
    </>}
  </div>;
}
