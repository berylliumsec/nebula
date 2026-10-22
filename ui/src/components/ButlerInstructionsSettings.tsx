import {useEffect, useState, type ChangeEvent} from "react";
import {createPortal} from "react-dom";
import {Eye, FileText, PencilLine, Upload, X} from "lucide-react";
import type {ApiClient} from "../api/client";
import {getButlerInstructions, updateButlerInstructions, type ButlerInstructionsFile} from "../api/butlerInstructions";
import {CodeMirrorSurface} from "./CodeMirrorSurface";
import {ModalSurface} from "./DialogSystem";
import {EditorMarkdownPreview} from "./EditorMarkdownPreview";
import {announceSettingsSaved} from "./SettingsSaveFeedback";
import {StatusChip} from "./SurfacePrimitives";
import {logCaughtDiagnostic} from "../diagnostics/logger";
import styles from "./ButlerInstructionsSettings.module.css";

const MAX_FILE_BYTES = 32_768;

interface Props {
  api?: ApiClient;
  previewMode?: boolean;
}

export function ButlerInstructionsSettings({api, previewMode = false}: Props) {
  const [saved, setSaved] = useState<ButlerInstructionsFile>();
  const [draft, setDraft] = useState("");
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<"edit" | "preview">("preview");
  const [confirmClose, setConfirmClose] = useState(false);
  const [cursor, setCursor] = useState({line: 1, column: 1});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();

  useEffect(() => {
    if (!api || previewMode) return;
    const controller = new AbortController();
    setLoading(true);
    void getButlerInstructions(api, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        setSaved(next);
        setDraft(next.content ?? "");
        setError(undefined);
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        void logCaughtDiagnostic("interface.butler_instructions.load_failed", "Butler instructions could not be loaded.", caught, "butler");
        setError(caught instanceof Error ? caught.message : "Could not load Butler instructions.");
      })
      .finally(() => {if (!controller.signal.aborted) setLoading(false);});
    return () => controller.abort();
  }, [api, previewMode]);

  const dirty = saved !== undefined && draft !== (saved.content ?? "");
  const bytes = new TextEncoder().encode(draft).length;
  const canSave = dirty && !!draft.trim() && bytes <= MAX_FILE_BYTES && !saving && !loading && !!api && !previewMode;

  const openFile = () => {
    if (!saved || loading || previewMode) return;
    setView(saved.status === "ready" ? "preview" : "edit");
    setConfirmClose(false);
    setOpen(true);
  };

  const closeFile = () => {
    if (dirty) {
      setConfirmClose(true);
      return;
    }
    setOpen(false);
    setConfirmClose(false);
  };

  const loadSaved = async () => {
    if (!api) return;
    setLoading(true);
    setError(undefined);
    setNotice(undefined);
    try {
      const next = await getButlerInstructions(api);
      setSaved(next);
      setDraft(next.content ?? "");
      setView(next.status === "ready" ? "preview" : "edit");
      setConfirmClose(false);
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_instructions.reload_failed", "Butler instructions could not be reloaded.", caught, "butler");
      setError(caught instanceof Error ? caught.message : "Could not reload Butler instructions.");
    } finally {
      setLoading(false);
    }
  };

  const upload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (dirty) {
      setError("Save or discard the open draft before uploading another file.");
      return;
    }
    setError(undefined);
    setNotice(undefined);
    if (file.size > MAX_FILE_BYTES) {
      setError("BUTLER.md must be at most 32 KiB.");
      return;
    }
    try {
      const content = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer());
      if (!content.trim()) {
        setError("BUTLER.md needs non-empty instructions.");
        return;
      }
      setDraft(content);
      setView("edit");
      setOpen(true);
      setNotice(`Loaded ${file.name}. Review the text, then save it to Core.`);
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_instructions.upload_failed", "The selected Butler file could not be read as UTF-8.", caught, "butler");
      setError(caught instanceof Error ? caught.message : "Could not read the selected file.");
    }
  };

  const save = async () => {
    if (!api || !saved || !canSave) return;
    setSaving(true);
    setError(undefined);
    setNotice(undefined);
    try {
      const updated = await updateButlerInstructions(api, draft, saved.sha256);
      const observed = await getButlerInstructions(api);
      if (observed.status !== "ready" || observed.sha256 !== updated.sha256 || observed.content !== draft) {
        throw new Error("Core did not confirm the saved Butler instructions. Reload before editing again.");
      }
      setSaved(observed);
      setDraft(observed.content ?? "");
      announceSettingsSaved("Butler instructions saved on Core.");
      setNotice("Saved on Core. Butler will read this version at its next scheduled check.");
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_instructions.save_failed", "Butler instructions could not be confirmed on Core.", caught, "butler");
      const message = caught instanceof Error ? caught.message : "Could not save Butler instructions.";
      setError(message.includes("changed since") || message.includes("409")
        ? "BUTLER.md changed elsewhere. Your draft is still here; copy it if needed, then reload the saved file before saving."
        : message);
    } finally {
      setSaving(false);
    }
  };

  return <section id="butler-instructions-settings" className={styles.section} aria-labelledby="butler-instructions-title">
    <div className="section-heading"><div><h2 id="butler-instructions-title">Butler instructions</h2><p>One Nebula-wide file guides Butler's scheduled checks, including idle projects.</p></div></div>
    <p>Butler chooses work across projects. Changes require Act authority in each target project. Child projects begin in Watch.</p>
    {previewMode && <p role="status">Connect to Core to manage Butler instructions.</p>}
    {loading && <p role="status">Loading Butler instructions…</p>}
    {error && !open && <p role="alert">{error}</p>}
    {notice && !open && <p role="status">{notice}</p>}
    <div className={styles.fileRow}>
      <button type="button" className={styles.fileButton} onClick={openFile} disabled={!api || previewMode || loading || !saved} aria-label={saved?.status === "missing" ? "Create BUTLER.md in editor" : "Open BUTLER.md in editor"}>
        <FileText size={22} aria-hidden="true" />
        <span><strong>BUTLER.md</strong><small>{saved?.status === "ready" ? "Open in Markdown editor" : "Create in Markdown editor"}</small></span>
      </button>
      <StatusChip tone={saved?.status === "ready" ? "success" : saved?.status === "invalid" ? "warning" : "neutral"}>{saved?.status === "ready" ? "Ready" : saved?.status === "invalid" ? "Needs attention" : "Not configured"}</StatusChip>
    </div>
    {saved?.status === "missing" && <p role="status">No instructions file yet. Create or upload one to enable the global check.</p>}
    {saved?.status === "invalid" && <p role="alert">{saved.detail ?? "The saved file cannot be used."}</p>}
    {saved?.path && <p className={styles.path}>Core data directory · <code>{saved.path}</code></p>}
    <div className={styles.actions}>
      <label className="button secondary" htmlFor="butler-instructions-upload"><Upload size={15} aria-hidden="true" /> Upload Markdown</label>
      <input id="butler-instructions-upload" type="file" accept=".md,text/markdown,text/plain" onChange={(event) => void upload(event)} disabled={!api || previewMode || loading || saving || !saved} aria-label="Upload Butler Markdown file" />
      <span className={styles.note}>UTF-8 Markdown · 32 KiB maximum · saved only on Core</span>
    </div>
    {open && saved && createPortal(<ModalSurface className={`${styles.dialog} complex-dialog`} labelledBy="butler-instructions-editor-title" onClose={closeFile}>
      <header className={styles.dialogHeader}>
        <div><small>NEBULA · BUTLER</small><h2 id="butler-instructions-editor-title"><FileText size={19} aria-hidden="true" /> BUTLER.md</h2></div>
        <button type="button" className="icon-button subtle" aria-label="Close Butler editor" title="Close Butler editor" onClick={closeFile}><X size={18} aria-hidden="true" /></button>
      </header>
      <div className={styles.editorToolbar}>
        <span className={styles.editorPath} title={saved.path}>{saved.path}</span>
        <div className="code-editor-markdown-actions" role="group" aria-label="Markdown view">
          <button className="icon-button subtle" type="button" aria-label="Edit Markdown" title="Edit Markdown" aria-pressed={view === "edit"} onClick={() => setView("edit")}><PencilLine size={18} aria-hidden="true" /></button>
          <button className="icon-button subtle" type="button" aria-label="Preview Markdown" title="Preview Markdown" aria-pressed={view === "preview"} onClick={() => setView("preview")}><Eye size={18} aria-hidden="true" /></button>
        </div>
      </div>
      {saved.status === "invalid" && <p className={styles.dialogNotice} role="alert">{saved.detail ?? "The saved file cannot be used."}</p>}
      {error && <p className={styles.dialogNotice} role="alert">{error}</p>}
      {notice && <p className={styles.dialogNotice} role="status">{notice}</p>}
      {confirmClose && <div className={styles.closePrompt} role="alert"><span>Discard unsaved changes?</span><button type="button" className="button quiet" onClick={() => setConfirmClose(false)}>Keep editing</button><button type="button" className="button danger" onClick={() => {setDraft(saved.content ?? ""); setOpen(false); setConfirmClose(false);}}>Discard draft</button></div>}
      <div className={styles.editorBody}>
        {view === "preview" ? <EditorMarkdownPreview content={draft} filePath="BUTLER.md" /> : <CodeMirrorSurface active ariaLabel="BUTLER.md" filePath="BUTLER.md" value={draft} onChange={(content) => {setDraft(content); setNotice(undefined);}} onCursorChange={(line, column) => setCursor({line, column})} onSave={() => void save()} wordWrap minimap={false} stickyScroll={false} />}
      </div>
      <footer className={styles.dialogFooter}>
        <span className={styles.editorMeta}>{dirty ? "Unsaved changes" : "Saved version"} · {bytes.toLocaleString()} / {MAX_FILE_BYTES.toLocaleString()} bytes {view === "edit" && `· Ln ${cursor.line}, Col ${cursor.column}`}</span>
        <div className={styles.footerActions}><button type="button" className="button quiet" onClick={() => void loadSaved()} disabled={!api || loading || saving}>{dirty ? "Discard edits and reload" : "Refresh saved file"}</button><button type="button" className="button primary" onClick={() => void save()} disabled={!canSave}>{saving ? "Saving…" : "Save to Core"}</button></div>
      </footer>
    </ModalSurface>, document.body)}
  </section>;
}
