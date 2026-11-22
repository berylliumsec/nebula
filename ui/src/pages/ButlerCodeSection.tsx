import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import { boardElapsed } from "../api/butlerBoard";
import { fileMarks, getFileAttributionIfChanged, getFileHistory, type FileAttribution, type FileChangeEvent } from "../api/fileAttribution";
import { startVisiblePoll } from "../api/visiblePoll";
import type { AgentChangeMark } from "../components/CodeMirrorSurface";
import { LoadingSurface } from "../components/SurfacePrimitives";
import { logCaughtDiagnostic } from "../diagnostics";
import { useWorkspace } from "../state/WorkspaceContext";
import styles from "./ButlerCodeSection.module.css";

const CodeEditorPanel = lazy(() => import("../components/CodeEditorPanel").then((module) => ({ default: module.CodeEditorPanel })));

function changeMarksFor(events: FileChangeEvent[]): AgentChangeMark[] {
  // Newest first: a line keeps the most recent chat's mark.
  const marks: AgentChangeMark[] = [];
  for (const event of events) {
    if (event.kind === "deleted") break;
    for (const hunk of event.hunks ?? []) {
      if (!hunk.lines) continue;
      marks.push({ start: hunk.start, lines: hunk.lines, kind: hunk.kind, title: `${hunk.kind === "added" ? "Added" : "Changed"} by ${event.sessionTitle}${event.turnIndex ? ` · turn ${event.turnIndex}` : ""}` });
    }
    // Older diffs no longer line up once the file was replaced.
    if (event.kind === "created") break;
  }
  return marks;
}

/**
 * Code & files inside Butler mode: the project's editor, with the files and
 * lines chats changed marked, and who changed the open file beside it. Core
 * owns the attribution; the editor keeps its own tabs and drafts.
 */
export function ButlerCodeSection({ api, projectId, openPath, onOpenBrief, onAskAbout }: {
  api: ApiClient;
  projectId: string;
  openPath?: string;
  onOpenBrief: (sessionId: string) => void;
  onAskAbout: (path: string) => void;
}) {
  const { engagements, providers, harnesses } = useWorkspace();
  const project = engagements.find((item) => item.id === projectId);
  const [files, setFiles] = useState<FileAttribution[]>([]);
  const [filesError, setFilesError] = useState("");
  const [activePath, setActivePath] = useState<string>();
  const [history, setHistory] = useState<{ path: string; events?: FileChangeEvent[]; error?: string }>();
  const etag = useRef<string | undefined>(undefined);
  const [clock, setClock] = useState(() => Date.now());

  useEffect(() => { const timer = window.setInterval(() => setClock(Date.now()), 30_000); return () => window.clearInterval(timer); }, []);

  useEffect(() => {
    const controller = new AbortController();
    etag.current = undefined;
    setFiles([]);
    startVisiblePoll({
      intervalMs: 10_000,
      maxIntervalMs: 60_000,
      signal: controller.signal,
      read: async (signal) => {
        try {
          const answer = await getFileAttributionIfChanged(api, projectId, etag.current, signal);
          setFilesError("");
          if (!answer) return "idle";
          etag.current = answer.etag;
          setFiles(answer.files);
          return answer.files.some((file) => file.state === "working") ? "active" : "idle";
        } catch (caught) {
          if (signal.aborted) return "stop";
          void logCaughtDiagnostic("interface.butler_mode.file_attribution_failed", "Butler mode could not read which chats changed files.", caught, "butler_mode");
          setFilesError(caught instanceof Error ? caught.message : "Core could not read file changes.");
          return "idle";
        }
      },
    });
    return () => controller.abort();
  }, [api, projectId]);

  const latestForActive = files.find((file) => file.path === activePath);
  useEffect(() => {
    if (!activePath) { setHistory(undefined); return; }
    const controller = new AbortController();
    setHistory((current) => current?.path === activePath ? current : { path: activePath });
    void getFileHistory(api, projectId, activePath, controller.signal).then((events) => {
      setHistory({ path: activePath, events });
    }).catch((caught: unknown) => {
      if (controller.signal.aborted) return;
      void logCaughtDiagnostic("interface.butler_mode.file_history_failed", "Butler mode could not read a file's change history.", caught, "butler_mode");
      setHistory({ path: activePath, error: caught instanceof Error ? caught.message : "Core could not read this file's history." });
    });
    return () => controller.abort();
    // A new change to the open file (its latest change time moves) reads the history again.
  }, [api, projectId, activePath, latestForActive?.changedAt]);

  const marks = useMemo(() => fileMarks(files), [files]);
  const changeMarks = useMemo(() => history?.events ? { [history.path]: changeMarksFor(history.events) } : undefined, [history]);
  const onActiveFileChange = useCallback((path: string | undefined) => setActivePath(path), []);
  const latest = history?.events?.[0];

  return <div className={styles.section}>
    <div className={styles.editor}>
      <Suspense fallback={<LoadingSurface label="Loading code editor" />}>
        <CodeEditorPanel key={projectId} active api={api} engagementId={projectId} editorSessionKey={`butler-mode:${projectId}`} workspacePath={project?.workspacePath} providers={providers} harnesses={harnesses} initialOpenPath={openPath} initialOpenRequest={openPath ? `butler:${openPath}` : undefined} fileMarks={marks} changeMarks={changeMarks} onActiveFileChange={onActiveFileChange} />
      </Suspense>
    </div>
    <aside className={styles.activity} aria-label="Who changed this file">
      <h2>Who changed this file</h2>
      {filesError && <p className={styles.error} role="alert">{filesError} Retrying on its own.</p>}
      {!activePath && <p className={styles.muted}>Open a file to see which chats changed it. Dots in the file list mark files chats changed this week.</p>}
      {activePath && !history?.events && !history?.error && <p className={styles.muted} role="status">Reading changes…</p>}
      {history?.error && <p className={styles.error} role="alert">{history.error}</p>}
      {history?.events && history.events.length === 0 && <p className={styles.muted}>No chat changed {activePath?.split("/").pop()} in the last 30 days.</p>}
      {latest && <p className={styles.muted}>Last changed by {latest.sessionTitle} {boardElapsed(latest.at, clock) ?? ""} ago.</p>}
      <ol className={styles.events}>{history?.events?.map((event, index) => <li key={`${event.sessionId}:${event.turnId ?? index}:${event.at}`} className={styles.event}>
        <div className={styles.eventHead}><i className={styles.dot} data-kind={event.kind} aria-hidden="true" /><strong>{event.sessionTitle}{event.turnIndex ? ` · turn ${event.turnIndex}` : ""}</strong><span className={styles.muted}>{boardElapsed(event.at, clock)}</span></div>
        <p className={styles.muted}>{event.kind === "created" ? "Created the file" : event.kind === "deleted" ? "Deleted the file" : "Changed the file"}{event.added !== undefined || event.removed !== undefined ? ` · +${event.added ?? 0} −${event.removed ?? 0}` : ""}{event.attribution === "uncertain" ? " · another chat ran at the same time" : ""}</p>
        <button type="button" className={styles.link} onClick={() => onOpenBrief(event.sessionId)}>Open brief</button>
      </li>)}</ol>
      {activePath && <button type="button" className={styles.ask} onClick={() => onAskAbout(activePath)}>Ask Butler about this file</button>}
      <p className={styles.muted}>Bars beside line numbers: green lines a chat added, orange lines it changed. Chats see your edits on their next turn.</p>
    </aside>
  </div>;
}
