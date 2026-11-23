import type { ChatWorkStage } from "../api/types";
import { chatStageDescription, chatStageName, chatStageState } from "../pages/workStages";

/**
 * A chat's project Work stage, named as the project defines it. The dot uses
 * the Projects page stage-tile colours for the assigned item's state.
 */
export function WorkStageLabel({ stage, className }: { stage: ChatWorkStage; className?: string }) {
  return <span className={`chat-work-stage${className ? ` ${className}` : ""}`} data-state={chatStageState(stage.itemStatus)} data-stage-key={stage.key} title={chatStageDescription(stage)}>
    <i aria-hidden="true" />
    <span className="chat-work-stage-text" aria-hidden="true">{stage.label}</span>
    <span className="sr-only">{chatStageName(stage)}</span>
  </span>;
}
