/** A passive view of the assistant's host page; never forwards operator input. */
export function BrowserPageSurface({ frame, connected }: { frame: string; connected: boolean }) {
  return <div className="managed-browser-screen" role="region" aria-label="Read-only browser viewport" tabIndex={0} aria-busy={!connected}>
    {frame ? <img src={frame} alt="Assistant browser page — read-only. Give directions in the Assistant." draggable={false} />
      : <p>{connected ? "Waiting for the assistant's page…" : "The page will appear when the Assistant browser connects."}</p>}
  </div>;
}
