import { AlertCircle, ArchiveRestore, LockKeyhole, MessageSquare, Plus, RefreshCw } from "lucide-react";
import type { ConversationLinkState } from "../pages/conversationLink";
import { SurfaceState } from "./SurfacePrimitives";

interface ConversationLinkNoticeProps {
  state: ConversationLinkState;
  restoring?: boolean;
  onNewChat: () => void;
  onRetry: () => void;
  onRestoreProject: (projectId: string) => void;
}

/** Stands in for the transcript while a conversation link opens or cannot. */
export function ConversationLinkNotice({ state, restoring = false, onNewChat, onRetry, onRestoreProject }: ConversationLinkNoticeProps) {
  if (state.status === "resolving") {
    return <div className="chat-link-state" role="status">
      <SurfaceState kind="loading" className="chat-empty-state" title="Opening conversation…" explanation="Finding the project this conversation belongs to." />
    </div>;
  }
  const newChat = <button className={`button ${state.status === "archived" || state.status === "failed" ? "secondary" : "primary"}`} type="button" onClick={onNewChat}><Plus size={15} aria-hidden="true" /> Start new chat</button>;
  if (state.status === "archived") {
    return <div className="chat-link-state" role="alert">
      <SurfaceState
        kind="unsupported"
        className="chat-empty-state"
        icon={<ArchiveRestore size={24} aria-hidden="true" />}
        title="Conversation is in an archived project"
        explanation={state.restoreError
          ? `${state.project.name} could not be restored. ${state.restoreError} Try again.`
          : `It belongs to ${state.project.name}. Restore the project to open it.`}
        primaryAction={<button className="button primary" type="button" disabled={restoring} onClick={() => onRestoreProject(state.project.id)}><ArchiveRestore size={15} aria-hidden="true" /> {restoring ? "Restoring…" : "Restore project"}</button>}
        secondaryAction={newChat}
      />
    </div>;
  }
  if (state.status === "failed") {
    return <div className="chat-link-state" role="alert">
      <SurfaceState
        kind="failure"
        className="chat-empty-state"
        icon={<AlertCircle size={24} aria-hidden="true" />}
        title="Conversation could not be opened"
        explanation={`${state.message} Your other conversations are unaffected.`}
        primaryAction={<button className="button primary" type="button" onClick={onRetry}><RefreshCw size={15} aria-hidden="true" /> Try again</button>}
        secondaryAction={newChat}
      />
    </div>;
  }
  const copy = state.status === "forbidden"
    ? { icon: LockKeyhole, title: "Conversation unavailable", explanation: "Nebula Core did not allow this device to open it. Choose another conversation or start a new chat." }
    : state.status === "unknown_project"
      ? { icon: MessageSquare, title: "Conversation unavailable", explanation: "Its project is not available on this Core. Choose another conversation or start a new chat." }
      : { icon: MessageSquare, title: "Conversation not found", explanation: "It was deleted or the link is incomplete. Choose another conversation or start a new chat." };
  const Icon = copy.icon;
  return <div className="chat-link-state" role="alert">
    <SurfaceState kind="empty" className="chat-empty-state" icon={<Icon size={24} aria-hidden="true" />} title={copy.title} explanation={copy.explanation} primaryAction={newChat} />
  </div>;
}
