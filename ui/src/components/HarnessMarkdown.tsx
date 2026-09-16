import { AssistantMarkdown } from "./AssistantMarkdown";

const noRunnableLanguages = new Set<never>();
const ignoreRun = () => undefined;

/** Render public harness prose with the same safe Markdown surface as chat replies. */
export function HarnessMarkdown({ content }: { content: string }) {
  return <div className="harness-reasoning-summary" tabIndex={0}>
    <AssistantMarkdown
      content={content}
      durable={false}
      runnableLanguages={noRunnableLanguages}
      onRun={ignoreRun}
    />
  </div>;
}
