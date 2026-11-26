import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import styles from "./ButlerMarkdown.module.css";

// Wide Butler tables scroll inside their own region instead of squeezing
// columns until each cell wraps a word per line.
const components: Components = {
  table: ({ node: _node, children, ...properties }) => <div className={styles.tableScroll} role="region" aria-label="Butler response table" tabIndex={0}>
    <table {...properties}>{children}</table>
  </div>,
};

/** A Butler answer rendered as GitHub-flavoured Markdown with readable tables, code and lists. */
export const ButlerMarkdown = memo(function ButlerMarkdown({ content }: { content: string }) {
  return <div className={styles.markdown}>
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{content}</ReactMarkdown>
  </div>;
});
