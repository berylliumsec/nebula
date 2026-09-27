import type { ReasoningEffort } from "../../api/types";
import { REASONING_EFFORTS } from "../../api/types";

interface SubagentEffortFieldProps {
  effort?: ReasoningEffort;
  disabled?: boolean;
  onChange: (effort: ReasoningEffort | undefined) => void;
}

export function SubagentEffortField({ effort, disabled = false, onChange }: SubagentEffortFieldProps) {
  return <div className="chat-settings-fields chat-subagent-effort">
    <label>
      <span>Subagent effort</span>
      <select
        aria-label="Subagent effort"
        value={effort ?? ""}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value ? event.target.value as ReasoningEffort : undefined)}
      >
        <option value="">Model choice</option>
        {REASONING_EFFORTS.map((level) => <option value={level} key={level}>{level[0].toUpperCase() + level.slice(1)}</option>)}
      </select>
    </label>
    <small>{effort ? "Every new subagent uses this level." : "The delegating model chooses each subagent's effort."}</small>
  </div>;
}
