/** Guide copy is plain text; `backticks` mark paths, fields and commands. */
export function GuideText({ text }: { text: string }) {
  return <>{text.split(/(`[^`]+`)/).map((part, index) => part.startsWith("`") && part.endsWith("`") && part.length > 1
    ? <code key={index}>{part.slice(1, -1)}</code>
    : part)}</>;
}
