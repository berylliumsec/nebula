/** Ticket-free state returned when the requesting browser lists Project terminals. */
export interface TerminalSessionSummary {
  sessionId: string;
  createdAt: string;
  state: "pending" | "claimed" | "launching" | "running" | "stopping";
  attached: boolean;
}

export interface TerminalSessionList {
  engagementId: string;
  sessions: TerminalSessionSummary[];
}

interface WireTerminalSessionSummary {
  session_id: string;
  created_at: string;
  state: TerminalSessionSummary["state"];
  attached: boolean;
}

export interface WireTerminalSessionList {
  engagement_id: string;
  sessions: WireTerminalSessionSummary[];
}

/** Select only ticket-free fields from Core's terminal response. */
export function mapTerminalSessionList(value: WireTerminalSessionList): TerminalSessionList {
  return {
    engagementId: value.engagement_id,
    sessions: value.sessions.map((session) => ({
      sessionId: session.session_id,
      createdAt: session.created_at,
      state: session.state,
      attached: session.attached,
    })),
  };
}
