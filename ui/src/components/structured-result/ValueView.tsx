import { useState } from "react";
import { Check, Copy, Eye, EyeOff } from "lucide-react";
import { StatusChip } from "../SurfacePrimitives";
import { logCaughtDiagnostic } from "../../diagnostics";
import { copySelectionText } from "../selection/selectionActions";
import { formatValue, type FormatHint, type FormattedValue } from "./format";

/** Copy exactly what a producer published, never the display form. */
export function CopyAction({ text, label, className = "" }: { text: string; label: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const copy = async () => {
    try {
      await copySelectionText(text);
      setFailed(false);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1_500);
    } catch (error) {
      void logCaughtDiagnostic("interface.structured_result.copy_failed", "A structured-result value could not be copied.", error, "structured_result");
      setFailed(true);
    }
  };
  return <button
    type="button"
    className={`icon-button subtle structured-copy ${className}`.trim()}
    aria-label={failed ? `${label} — copy failed, select the text instead` : copied ? `${label} — copied` : label}
    title={failed ? "Copy failed. Select the text and copy it manually." : copied ? "Copied" : label}
    onClick={() => void copy()}
  >
    {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
  </button>;
}

interface ScalarValueProps {
  name?: string | number;
  value: unknown;
  format?: FormatHint;
  /** A hint asked for this field to be masked; the value stays one click away. */
  redacted?: boolean;
  /** A sibling field named the language of this text, if one did. */
  language?: string;
  className?: string;
}

/** Lines of a multi-line value shown before it has to be expanded. */
const BLOCK_PREVIEW_LINES = 12;

/**
 * Multi-line text — a code snippet, a disassembly, a stack, an explanation —
 * read as a block rather than a truncated line. The text is rendered as text:
 * nothing in a result is ever interpreted as markup.
 */
export function TextBlock({ text, language }: { text: string; language?: string }) {
  const [expanded, setExpanded] = useState(false);
  const lines = text.split("\n");
  const clipped = !expanded && lines.length > BLOCK_PREVIEW_LINES;
  return <div className="structured-block">
    <div className="structured-block-head">
      <span className="structured-block-language">{language ? language : `${lines.length} lines`}</span>
      <CopyAction text={text} label="Copy text" />
    </div>
    <pre tabIndex={0}><code>{clipped ? `${lines.slice(0, BLOCK_PREVIEW_LINES).join("\n")}\n` : text}</code></pre>
    {lines.length > BLOCK_PREVIEW_LINES && <button type="button" className="structured-inline-button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
      {expanded ? "Show less" : `Show all ${lines.length} lines`}
    </button>}
  </div>;
}

function Rendered({ formatted }: { formatted: FormattedValue }) {
  if (formatted.kind === "null") {
    return <span className="structured-absent" title={formatted.full}>{formatted.display}</span>;
  }
  if (formatted.kind === "badge") {
    return <StatusChip tone="neutral" className="structured-badge">{formatted.display}</StatusChip>;
  }
  if (formatted.kind === "boolean") {
    return <span className="structured-boolean" data-value={formatted.full}>{formatted.display}</span>;
  }
  if (formatted.kind === "url" && formatted.href) {
    return <a href={formatted.href} target="_blank" rel="noreferrer noopener external" title={formatted.title}>{formatted.display}</a>;
  }
  return <span className={formatted.monospace ? "structured-mono" : undefined} title={formatted.title}>{formatted.display}</span>;
}

/**
 * One scalar, formatted conservatively. Truncation is always reversible in
 * place, and the exact value is on the copy action beside it.
 */
export function ScalarValue({ name, value, format, redacted = false, language, className = "" }: ScalarValueProps) {
  const [expanded, setExpanded] = useState(false);
  const [revealed, setRevealed] = useState(false);
  const formatted = formatValue(name, value, format);

  if (redacted && !revealed) {
    return <span className={`structured-value structured-redacted ${className}`.trim()}>
      <span className="structured-mono" aria-hidden="true">••••••••</span>
      <span className="sr-only">Value hidden by a presentation hint</span>
      <button type="button" className="structured-inline-button" onClick={() => setRevealed(true)}>
        <Eye size={13} aria-hidden="true" /> Reveal
      </button>
    </span>;
  }

  if (typeof value === "string" && value.includes("\n")) {
    return <TextBlock text={value} language={language} />;
  }

  return <span className={`structured-value ${className}`.trim()} data-kind={formatted.kind}>
    {expanded && formatted.truncated
      ? <span className={formatted.monospace ? "structured-mono structured-expanded" : "structured-expanded"}>{formatted.full}</span>
      : <Rendered formatted={formatted} />}
    {formatted.truncated && <button type="button" className="structured-inline-button" onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>
      {expanded ? "Show less" : "Show all"}
    </button>}
    {redacted && revealed && <button type="button" className="structured-inline-button" onClick={() => setRevealed(false)}>
      <EyeOff size={13} aria-hidden="true" /> Hide
    </button>}
  </span>;
}
