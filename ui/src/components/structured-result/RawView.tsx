import { useEffect, useMemo, useRef, useState } from "react";
import { Download, Search } from "lucide-react";
import { logCaughtDiagnostic } from "../../diagnostics";
import { CopyAction } from "./ValueView";
import { MAX_RAW_LINES, renderRaw } from "./rawText";

/** Lines rendered at once; the rest stay one control away. */
const WINDOW = 1_000;

interface RawViewProps {
  value: unknown;
  title: string;
  /** A path the inspector asked to reveal, scrolled to and highlighted. */
  anchorPath?: string;
}

/**
 * The unmodified result, printed as JSON. Everything else in the dashboard is
 * derived from this text; this view is the one that shows it as published.
 */
export function RawView({ value, title, anchorPath }: RawViewProps) {
  const document_ = useMemo(() => renderRaw(value), [value]);
  const [query, setQuery] = useState("");
  const [start, setStart] = useState(0);
  const [match, setMatch] = useState(0);
  const anchorRef = useRef<HTMLSpanElement>(null);

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return [];
    const found: number[] = [];
    for (let index = 0; index < document_.lines.length && found.length < 5_000; index += 1) {
      if (document_.lines[index].toLowerCase().includes(needle)) found.push(index);
    }
    return found;
  }, [document_.lines, query]);

  const anchorLine = anchorPath ? document_.lineForPath.get(anchorPath) : undefined;
  useEffect(() => {
    if (anchorLine === undefined) return;
    setStart(Math.max(0, anchorLine - Math.floor(WINDOW / 4)));
  }, [anchorLine]);
  useEffect(() => {
    if (anchorLine === undefined) return;
    // Not every environment implements scrolling; the line is highlighted
    // either way, and a missing scroll must not take the view down.
    if (typeof anchorRef.current?.scrollIntoView === "function") anchorRef.current.scrollIntoView({ block: "center" });
  }, [anchorLine, start]);

  const jump = (step: number) => {
    if (matches.length === 0) return;
    const next = (match + step + matches.length) % matches.length;
    setMatch(next);
    setStart(Math.max(0, matches[next] - Math.floor(WINDOW / 4)));
  };

  const download = () => {
    try {
      const text = document_.truncated ? safeText(value) : `${document_.lines.join("\n")}\n`;
      const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
      const anchor = window.document.createElement("a");
      anchor.href = url;
      anchor.download = `${title.replace(/[^a-z0-9._-]+/gi, "-").toLowerCase() || "result"}.json`;
      window.document.body.append(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (error) {
      void logCaughtDiagnostic("interface.structured_result.download_failed", "A structured result could not be downloaded.", error, "structured_result");
    }
  };

  const end = Math.min(document_.lines.length, start + WINDOW);
  const visible = document_.lines.slice(start, end);

  return <div className="structured-raw">
    <header className="structured-raw-header">
      <div>
        <h3>Raw result</h3>
        <p className="structured-derived">The result exactly as it was published. Every other view is derived from this text.</p>
      </div>
      <div className="structured-raw-actions">
        <CopyAction text={document_.truncated ? safeText(value) : document_.lines.join("\n")} label="Copy raw JSON" />
        <button type="button" className="button quiet" onClick={download}><Download size={15} aria-hidden="true" /> Download</button>
      </div>
    </header>

    <div className="structured-raw-controls">
      <label className="structured-search">
        <Search size={15} aria-hidden="true" />
        <span className="sr-only">Search the raw result</span>
        <input type="search" value={query} placeholder="Search raw JSON" onChange={(event) => { setQuery(event.target.value); setMatch(0); if (event.target.value.trim()) setStart(0); }} />
      </label>
      {query.trim() && <p role="status">
        {matches.length === 0 ? "No match" : `Match ${match + 1} of ${matches.length}${matches.length >= 5_000 ? "+" : ""}`}
      </p>}
      {matches.length > 0 && <>
        <button type="button" className="button quiet" onClick={() => jump(-1)}>Previous match</button>
        <button type="button" className="button quiet" onClick={() => jump(1)}>Next match</button>
      </>}
    </div>

    {document_.truncated && <p className="structured-derived" role="status">
      The printed text stops after {MAX_RAW_LINES.toLocaleString()} lines. Download the file for the complete result.
    </p>}

    <pre className="structured-raw-text" tabIndex={0} aria-label="Raw result text">
      {visible.map((line, index) => {
        const number = start + index;
        const highlighted = matches.includes(number);
        const anchored = anchorLine === number;
        return <span
          key={number}
          ref={anchored ? anchorRef : undefined}
          className="structured-raw-line"
          data-match={highlighted ? "true" : undefined}
          data-anchor={anchored ? "true" : undefined}
        >{line}{"\n"}</span>;
      })}
    </pre>

    <div className="structured-raw-footer">
      <p role="status">Lines {document_.lines.length === 0 ? 0 : start + 1}–{end} of {document_.lines.length.toLocaleString()}</p>
      <button type="button" className="button quiet" disabled={start === 0} onClick={() => setStart(Math.max(0, start - WINDOW))}>Earlier lines</button>
      <button type="button" className="button quiet" disabled={end >= document_.lines.length} onClick={() => setStart(start + WINDOW)}>Later lines</button>
    </div>
  </div>;
}

function safeText(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? "null";
  } catch (error) {
    void logCaughtDiagnostic("interface.structured_result.raw_encode_failed", "A structured result could not be encoded as JSON text.", error, "structured_result");
    return String(value);
  }
}
