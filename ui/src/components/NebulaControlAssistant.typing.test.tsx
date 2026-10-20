import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { useWorkspace } from "../state/WorkspaceContext";
import { ThemeProvider } from "../state/ThemeContext";
import { DialogProvider } from "./DialogSystem";
import { NebulaControlAssistant } from "./NebulaControlAssistant";

const markdown = vi.hoisted(() => ({ renders: 0 }));

vi.mock("react-markdown", () => ({
  default: ({ children }: { children: ReactNode }) => {
    markdown.renders += 1;
    return <>{children}</>;
  },
}));
vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: vi.fn() }));
vi.mock("./ButlerOversight", () => ({ ButlerOversight: () => null }));
vi.mock("./ButlerMemory", () => ({ ButlerMemory: () => null }));
vi.mock("./useButlerOverview", () => ({ useButlerOverview: () => ({ overview: undefined, refresh: vi.fn() }) }));

beforeEach(() => {
  localStorage.clear();
  markdown.renders = 0;
});

it("does not reparse saved Butler messages while typing a draft", async () => {
  const history = Array.from({ length: 80 }, (_, index) => ({
    id: `message-${index}`, role: index % 2 ? "assistant" : "user", content: `Saved reply ${index}`,
  }));
  vi.mocked(useWorkspace).mockReturnValue({
    api: { getControlAssistantSession: vi.fn().mockResolvedValue({
      id: "control", engagementId: "project", backend: "harness", harnessProfileId: "local",
      model: "model", isControlAssistant: true,
    }), listChatMessages: vi.fn().mockResolvedValue(history) },
    engagement: { id: "project", name: "Project" }, providers: [],
    harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
      permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
  } as unknown as ReturnType<typeof useWorkspace>);
  const user = userEvent.setup();
  render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
  await user.click(screen.getByRole("button", { name: "Open Butler" }));
  expect(await screen.findByText("Saved reply 79")).toBeVisible();
  const composer = screen.getByRole("textbox", { name: "Ask Butler a question" });
  await waitFor(() => expect(composer).toBeEnabled());
  const initialRenders = markdown.renders;
  expect(initialRenders).toBeGreaterThanOrEqual(history.length);
  await user.type(composer, "Typing a new question");
  expect(composer).toHaveValue("Typing a new question");
  expect(markdown.renders).toBe(initialRenders);
});
