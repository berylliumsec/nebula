import { LSPClient, languageServerExtensions, type Transport } from "@codemirror/lsp-client";
import { websocketAuthProtocol } from "./events";

export type LanguageServerState = "connecting" | "ready" | "reconnecting" | "unavailable";

function socketUrl(apiBaseUrl: string, engagementId: string): string {
  const url = new URL(`${apiBaseUrl.replace(/\/$/, "")}/engagements/${encodeURIComponent(engagementId)}/language-server/ws`);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

function textOnlyHTML(html: string): string {
  const element = document.createElement("span");
  element.textContent = new DOMParser().parseFromString(html, "text/html").body.textContent ?? "";
  return element.innerHTML;
}

export function createLanguageServer(
  apiBaseUrl: string,
  engagementId: string,
  token: string | undefined,
  onState: (state: LanguageServerState) => void,
): { client: LSPClient; close(): void } {
  const client = new LSPClient({
    rootUri: "file:///workspace",
    extensions: languageServerExtensions(),
    sanitizeHTML: textOnlyHTML,
    timeout: 4_000,
  });
  if (typeof WebSocket === "undefined") {
    onState("unavailable");
    return { client, close: () => client.disconnect() };
  }
  const protocols = ["nebula.language-server.v1"];
  if (token) protocols.push(websocketAuthProtocol(token));
  let socket: WebSocket | undefined;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let attempt = 0;
  let stopped = false;
  const open = () => {
    onState(attempt ? "reconnecting" : "connecting");
    const current = new WebSocket(socketUrl(apiBaseUrl, engagementId), protocols);
    socket = current;
    const subscribers = new Set<(message: string) => void>();
    const transport: Transport = {
      send: (message) => current.send(message),
      subscribe: (handler) => subscribers.add(handler),
      unsubscribe: (handler) => subscribers.delete(handler),
    };
    current.addEventListener("open", () => {
      client.connect(transport);
      void client.initializing.then(() => { attempt = 0; onState("ready"); }, () => onState("unavailable"));
    });
    current.addEventListener("message", (event) => {
      if (typeof event.data === "string") subscribers.forEach((handler) => handler(event.data));
    });
    current.addEventListener("close", () => {
      client.disconnect();
      if (stopped) return;
      onState("unavailable");
      const delay = Math.min(1_000 * 2 ** attempt++, 15_000);
      retry = setTimeout(open, delay);
    });
  };
  open();
  return {
    client,
    close: () => {
      stopped = true;
      if (retry) clearTimeout(retry);
      client.disconnect();
      if (socket?.readyState === WebSocket.CONNECTING || socket?.readyState === WebSocket.OPEN) socket.close(1000, "Editor closed");
    },
  };
}
