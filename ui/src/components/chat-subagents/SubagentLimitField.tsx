import { useEffect, useId, useState } from "react";
import { SUBAGENT_LIMIT_MAX, subagentLimit } from "../../api/subagentLimits";

/** How a subagent toggle describes the limit, e.g. "up to 3 at a time". */
export function subagentLimitLabel(limit?: number): string {
  return limit ? `up to ${limit} at a time` : "no limit";
}

interface SubagentLimitFieldProps {
  limit?: number;
  /** Who does the delegating, e.g. "Codex" or "the assistant". */
  delegator: string;
  disabled?: boolean;
  onChange: (limit: number | undefined) => void;
}

/**
 * The operator's optional cap on how many subagents run at once. Empty means
 * no limit; Core refuses a start at the limit and tells the model to wait.
 */
export function SubagentLimitField({ limit, delegator, disabled = false, onChange }: SubagentLimitFieldProps) {
  const hintId = useId();
  const [draft, setDraft] = useState(limit ? String(limit) : "");
  useEffect(() => setDraft(limit ? String(limit) : ""), [limit]);
  const invalid = draft.trim() !== "" && subagentLimit(draft) === undefined;

  return <div className="chat-settings-fields chat-subagent-limit">
    <label>
      <span>Running at once</span>
      <input
        type="number"
        inputMode="numeric"
        min={1}
        max={SUBAGENT_LIMIT_MAX}
        step={1}
        placeholder="No limit"
        value={draft}
        disabled={disabled}
        aria-invalid={invalid || undefined}
        aria-describedby={hintId}
        onChange={(event) => {
          setDraft(event.target.value);
          const next = subagentLimit(event.target.value);
          if (!event.target.value.trim() || next !== undefined) onChange(next);
        }}
        onBlur={() => setDraft(limit ? String(limit) : "")}
      />
    </label>
    <small id={hintId} role={invalid ? "alert" : undefined} data-tone={invalid ? "error" : undefined}>
      {invalid
        ? `Enter a whole number from 1 to ${SUBAGENT_LIMIT_MAX}, or leave it empty for no limit.`
        : limit
          ? `Optional. With ${limit} running, a new start is refused until one finishes. Clear it for no limit.`
          : `Optional. Leave empty and ${delegator} may run as many subagents as it needs. With a number, Nebula refuses new ones until one finishes.`}
    </small>
  </div>;
}
