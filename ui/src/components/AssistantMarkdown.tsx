import { memo, useMemo, useRef, useState, type MouseEvent } from "react";
import { Check, Copy, Play } from "lucide-react";
import { Highlight, themes, type Language } from "prism-react-renderer";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ExecutionLanguage, ExecutionOrigin } from "../api/types";
import {
  parseExactFences,
  sha256,
  utf8Length,
  type ExactFence,
} from "./assistantCode";
import { logCaughtDiagnostic } from "../diagnostics";
import { copySelectionText } from "./selection/selectionActions";

export interface FencedRunCandidate {
  source: string;
  language: ExecutionLanguage;
  declaredLanguage: string;
  origin: ExecutionOrigin;
}

interface AssistantMarkdownProps {
  content: string;
  messageId?: string;
  durable: boolean;
  streaming?: boolean;
  runnableLanguages: ReadonlySet<ExecutionLanguage>;
  onRun: (candidate: FencedRunCandidate) => void;
  onRunInTerminal?: (candidate: FencedRunCandidate) => void;
}

function safeUrl(value: string): string {
  if (value.startsWith("#")) return value;
  try {
    const parsed = new URL(value);
    return ["http:", "https:", "mailto:"].includes(parsed.protocol) ? value : "";
  } catch (caughtError) {
    void logCaughtDiagnostic("interface.assistant_markdown.caught_failure_01", "A handled interface operation failed.", caughtError, "assistant_markdown");
    return "";
  }
}

function openSafeLink(event: MouseEvent<HTMLAnchorElement>, href?: string) {
  if (!href) {
    event.preventDefault();
    return;
  }
  if (href.startsWith("#")) return;
  event.preventDefault();
  globalThis.open(href, "_blank", "noopener,noreferrer");
}

function selectedOffsets(element: HTMLElement, source: string): { start: number; end: number } | undefined {
  const selection = globalThis.getSelection?.();
  if (!selection || selection.isCollapsed || selection.rangeCount !== 1) return undefined;
  const range = selection.getRangeAt(0);
  if (!element.contains(range.startContainer) || !element.contains(range.endContainer)) return undefined;
  const prefix = document.createRange();
  prefix.selectNodeContents(element);
  prefix.setEnd(range.startContainer, range.startOffset);
  const selected = document.createRange();
  selected.selectNodeContents(element);
  selected.setStart(range.startContainer, range.startOffset);
  selected.setEnd(range.endContainer, range.endOffset);
  const start = prefix.toString().length;
  const end = start + selected.toString().length;
  if (end <= start || source.slice(start, end) !== selected.toString()) return undefined;
  return { start, end };
}

function FencedCode({
  block,
  canRun,
  messageId,
  onRun,
  onRunInTerminal,
}: {
  block: ExactFence;
  canRun: boolean;
  messageId?: string;
  onRun: (candidate: FencedRunCandidate) => void;
  onRunInTerminal?: (candidate: FencedRunCandidate) => void;
}) {
  const codeRef = useRef<HTMLElement>(null);
  const selectionRef = useRef<{ start: number; end: number } | undefined>(undefined);
  const [feedback, setFeedback] = useState("");
  const language = (block.canonicalLanguage === "sh" ? "bash" : block.canonicalLanguage ?? "text") as Language;
  const lineBreaks = block.source.match(/\r\n|\r|\n/g) ?? [];
  const lineCount = Math.max(1, lineBreaks.length + (/(?:\r\n|\r|\n)$/.test(block.source) ? 0 : 1));

  const slice = () => {
    const current = codeRef.current ? selectedOffsets(codeRef.current, block.source) : undefined;
    if (current) selectionRef.current = current;
    const selected = current ?? selectionRef.current;
    return {
      source: selected ? block.source.slice(selected.start, selected.end) : block.source,
      selectionStartByte: selected ? utf8Length(block.source.slice(0, selected.start)) : undefined,
      selectionEndByte: selected ? utf8Length(block.source.slice(0, selected.end)) : undefined,
    };
  };

  const captureSelection = () => {
    const selected = codeRef.current ? selectedOffsets(codeRef.current, block.source) : undefined;
    if (selected) selectionRef.current = selected;
  };

  const copy = async () => {
    const selected = slice();
    try {
      await copySelectionText(selected.source);
      setFeedback("Copied exact source");
      globalThis.setTimeout(() => setFeedback(""), 1800);
    } catch (error) {
      // diagnostic-expected: clipboard denial is shown beside the retryable copy control.
      setFeedback(error instanceof Error ? error.message : "Copy failed. Select the code and copy it manually.");
    }
  };

  const run = async () => {
    if (!block.canonicalLanguage || !messageId) return;
    const selected = slice();
    const blockSha256 = await sha256(block.source);
    const candidate = {
      source: selected.source,
      language: block.canonicalLanguage,
      declaredLanguage: block.declaredLanguage,
      origin: {
        kind: "assistant_message",
        messageId,
        blockOrdinal: block.ordinal,
        blockSha256,
        selectionStartByte: selected.selectionStartByte,
        selectionEndByte: selected.selectionEndByte,
      },
    } satisfies FencedRunCandidate;
    if (["bash", "sh"].includes(candidate.language) && onRunInTerminal) onRunInTerminal(candidate);
    else onRun(candidate);
  };

  return (
    <div className="assistant-code-block">
      <header>
        <span>{block.declaredLanguage || "code"}</span>
        <div>
          <button type="button" onMouseDown={captureSelection} onClick={() => void copy()} aria-label="Copy exact code">
            {feedback === "Copied exact source" ? <Check size={13} /> : <Copy size={13} />} Copy
          </button>
          {canRun && (
            <button className="run-code" type="button" onMouseDown={captureSelection} onClick={() => void run()} aria-label={["bash", "sh"].includes(block.canonicalLanguage ?? "") && onRunInTerminal ? `Run ${block.canonicalLanguage} code in terminal` : `Review and run ${block.canonicalLanguage} code`}>
              <Play size={13} /> {["bash", "sh"].includes(block.canonicalLanguage ?? "") && onRunInTerminal ? "Run in terminal" : "Run"}
            </button>
          )}
        </div>
      </header>
      <Highlight theme={themes.github} code={block.source} language={language}>
        {({ style, tokens, getLineProps, getTokenProps }) => (
          <pre style={style}>
            <code ref={codeRef} onMouseUp={captureSelection}>
              {tokens.slice(0, lineCount).map((line, lineIndex) => (
                <span {...getLineProps({ line })} className="assistant-code-line" key={lineIndex}>
                  {line.map((token, tokenIndex) => (
                    <span {...getTokenProps({ token })} key={tokenIndex} />
                  ))}
                  {lineBreaks[lineIndex] ?? ""}
                </span>
              ))}
            </code>
          </pre>
        )}
      </Highlight>
      <span className={feedback === "Copied exact source" ? "sr-only" : "inline-error"} role="status" aria-live="polite">{feedback}</span>
    </div>
  );
}

export const AssistantMarkdown = memo(function AssistantMarkdown({
  content,
  messageId,
  durable,
  streaming = false,
  runnableLanguages,
  onRun,
  onRunInTerminal,
}: AssistantMarkdownProps) {
  const parsed = useMemo(() => parseExactFences(content), [content]);
  const renderable = parsed.unmatchedStart === undefined ? content : content.slice(0, parsed.unmatchedStart);
  const unmatched = parsed.unmatchedStart === undefined ? "" : content.slice(parsed.unmatchedStart);
  const claimed = new Set<number>();

  const components: Components = {
    pre: ({ children }) => <>{children}</>,
    code: ({ node, className, children, ...properties }) => {
      const offset = node?.position?.start.offset;
      let block = parsed.blocks.find((candidate) => candidate.openStart === offset);
      if (!block && className?.startsWith("language-")) {
        const rendered = String(children);
        block = parsed.blocks.find((candidate) => !claimed.has(candidate.ordinal)
          && (candidate.source === rendered || candidate.source.replace(/\n$/, "") === rendered.replace(/\n$/, "")));
      }
      if (block) {
        claimed.add(block.ordinal);
        return (
          <FencedCode
            block={block}
            messageId={messageId}
            canRun={Boolean(durable && messageId && block.canonicalLanguage && runnableLanguages.has(block.canonicalLanguage))}
            onRun={onRun}
            onRunInTerminal={onRunInTerminal}
          />
        );
      }
      return <code className={className} {...properties}>{children}</code>;
    },
    img: () => null,
    a: ({ node: _node, href, children, ...properties }) => {
      const safe = href ? safeUrl(href) : "";
      return <a {...properties} href={safe || undefined} rel="noopener noreferrer" onClick={(event) => openSafeLink(event, safe)}>{children}</a>;
    },
  };

  return (
    <div className={`assistant-markdown${streaming ? " streaming" : ""}`}>
      {renderable && (
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} urlTransform={safeUrl}>
          {renderable}
        </ReactMarkdown>
      )}
      {unmatched && <pre className="assistant-inert-fence">{unmatched}</pre>}
    </div>
  );
});
