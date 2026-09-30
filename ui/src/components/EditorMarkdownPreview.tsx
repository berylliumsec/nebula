import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

function safeMarkdownUrl(value: string): string {
  if (value.startsWith("#")) return value;
  try {
    const url = new URL(value);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? value : "";
  } catch {
    // Relative workspace paths cannot be served from the app's own origin.
    return "";
  }
}

const components: Components = {
  a: ({ node: _node, href, children, ...props }) => {
    const safe = href ? safeMarkdownUrl(href) : "";
    return safe
      ? <a {...props} href={safe} target={safe.startsWith("#") ? undefined : "_blank"} rel="noopener noreferrer">{children}</a>
      : <span title="Relative workspace links are unavailable in preview">{children}</span>;
  },
  img: ({ alt }) => <span className="code-editor-markdown-image" role="img" aria-label={alt || "Image"}>Image: {alt || "unavailable in preview"}</span>,
  input: ({ node: _node, ...props }) => props.type === "checkbox"
    ? <input {...props} aria-label={props.checked ? "Done" : "To do"} disabled />
    : <input {...props} disabled />,
};

export function EditorMarkdownPreview({ content, filePath }: { content: string; filePath: string }) {
  return <div className="code-editor-markdown-preview" role="region" aria-label={`Markdown preview: ${filePath}`} tabIndex={0}>
    <div className="assistant-markdown">
      {content.trim() ? <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} urlTransform={safeMarkdownUrl}>{content}</ReactMarkdown> : <p className="code-editor-markdown-empty">This Markdown file is empty.</p>}
    </div>
  </div>;
}
