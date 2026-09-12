import type { HarnessCommand } from "../api/types";

// New sessions have no peer yet. Core supplies the authoritative catalog once connected.
export const harnessCommands: HarnessCommand[] = [
  { name: "goal", description: "Set or inspect a goal", hint: "<objective> | status | pause | resume | clear", source: "nebula" },
  { name: "goals", description: "Alias for /goal", hint: "", source: "nebula" },
  { name: "usage", description: "Session usage", hint: "", source: "nebula" },
  { name: "help", description: "Command help", hint: "", source: "nebula" },
];

export function isHarnessCommand(text: string): boolean {
  return /^\/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}(?:\s|$)/.test(text.trim());
}

export function HarnessCommandHints({ draft, onSelect, commands = harnessCommands, discoveryPending = false }: {
  draft: string;
  onSelect: (text: string) => void;
  commands?: HarnessCommand[];
  discoveryPending?: boolean;
}) {
  if (!draft.startsWith("/") || /\s/.test(draft)) return null;
  const matches = commands.filter(({ name }) => `/${name}`.startsWith(draft));
  return <div className="harness-command-hints" aria-label="Harness commands">
    {matches.map(({ name, description, hint }) => <button className="button quiet" type="button" key={name} title={hint || description} aria-label={`/${name} ${description}`} onClick={() => onSelect(`/${name} `)}><strong>/{name}</strong><span>{description}{hint && <small>{hint}</small>}</span></button>)}
    {!matches.length && <span role="status">Command unavailable. Use /help to see available commands.</span>}
    {discoveryPending && <small>Use /help to connect and discover session commands.</small>}
  </div>;
}
