import {createServer, type Server} from "node:http";
import type {AddressInfo} from "node:net";

export interface LocalModelStub {
  origin: string;
  requests: Array<Record<string, unknown>>;
  server: Server;
  fail: boolean;
  responseContent?: string;
}

export async function startOpenaiResponsesStub(): Promise<LocalModelStub & { authorizations: string[] }> {
  const requests: Array<Record<string, unknown>> = [];
  const authorizations: string[] = [];
  const server = createServer(async (request, response) => {
    response.setHeader("Content-Type", "application/json");
    if (request.method === "GET" && request.url === "/v1/models") {
      response.end(JSON.stringify({object: "list", data: [{id: "gpt-daybreak-blue-latest", object: "model"}]}));
      return;
    }
    if (request.method === "POST" && request.url === "/v1/responses") {
      const chunks: Buffer[] = [];
      for await (const chunk of request) chunks.push(Buffer.from(chunk));
      requests.push(JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>);
      authorizations.push(request.headers.authorization ?? "");
      response.end(JSON.stringify({
        id: "resp_synthetic_daybreak", object: "response", model: "gpt-daybreak-blue-latest",
        status: "completed", output: [{type: "message", content: [{type: "output_text", text: "Synthetic Daybreak reply."}]}],
        usage: {input_tokens: 5, output_tokens: 4, total_tokens: 9},
      }));
      return;
    }
    response.statusCode = 404;
    response.end(JSON.stringify({error: {message: "Unknown synthetic OpenAI route"}}));
  });
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  return {origin: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, requests, authorizations, server, fail: false};
}

export async function startLocalModelStub(options: { fail?: boolean; streamDelayMs?: number; goalResponseDelayMs?: number; delayedMessage?: string; matchingDelayMs?: number; models?: string[]; responseContent?: string; beforeResponse?: (body: Record<string, unknown>) => Promise<void>; toolCall?: (body: Record<string, unknown>) => {name: string; arguments: Record<string, unknown>} | undefined } = {}): Promise<LocalModelStub> {
  const requests: Array<Record<string, unknown>> = [];
  const stub: LocalModelStub = { origin: "", requests, server: undefined as unknown as Server, fail: options.fail === true, responseContent: options.responseContent };
  const server = createServer(async (request, response) => {
    response.setHeader("Content-Type", "application/json");
    if (request.method === "GET" && request.url === "/v1/models") {
      response.end(JSON.stringify({
        object: "list",
        data: (options.models ?? ["security-model"]).map((id) => ({ id, object: "model", created: 1, owned_by: "local-acceptance" })),
      }));
      return;
    }
    if (request.method === "POST" && request.url === "/v1/chat/completions") {
      const chunks: Buffer[] = [];
      for await (const chunk of request) chunks.push(Buffer.from(chunk));
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>;
      requests.push(body);
      if (stub.fail) {
        response.statusCode = 503;
        response.end(JSON.stringify({ error: { message: "deliberate acceptance failure" } }));
        return;
      }
      const tools = Array.isArray(body.tools) ? body.tools as Array<{
        function?: { name?: string; parameters?: { properties?: { nonce?: { enum?: string[] } } } };
      }> : [];
      const capabilityProbe = tools.find((tool) => tool.function?.name === "nebula_capability_probe");
      const nonce = capabilityProbe?.function?.parameters?.properties?.nonce?.enum?.[0];
      if (nonce) {
        response.end(JSON.stringify({
          id: "chatcmpl-real-core-probe",
          object: "chat.completion",
          created: 1,
          model: "security-model",
          choices: [{
            index: 0,
            message: {
              role: "assistant",
              content: null,
              tool_calls: [{
                id: "call-real-core-probe",
                type: "function",
                function: { name: "nebula_capability_probe", arguments: JSON.stringify({ nonce }) },
              }],
            },
            finish_reason: "tool_calls",
          }],
          usage: { prompt_tokens: 12, completion_tokens: 4, total_tokens: 16 },
        }));
        return;
      }
      if (tools.some(tool => tool.function?.name === "finish_response")) {
        response.end(JSON.stringify({id: "chatcmpl-queue-routing", object: "chat.completion", created: 1, model: "security-model", choices: [{index: 0, message: {role: "assistant", content: null, tool_calls: [{id: "finish-queue", type: "function", function: {name: "finish_response", arguments: "{}"}}]}, finish_reason: "tool_calls"}], usage: {prompt_tokens: 12, completion_tokens: 4, total_tokens: 16}}));
        return;
      }
      const messages = Array.isArray(body.messages) ? body.messages as Array<{ content?: unknown }> : [];
      if (messages.some((message) => typeof message.content === "string" && message.content.includes("Name this conversation from its first exchange"))) {
        response.end(JSON.stringify({
          id: "chatcmpl-real-core-name",
          object: "chat.completion",
          created: 1,
          model: "security-model",
          choices: [{ index: 0, message: { role: "assistant", content: "Expired HTTPS Certificate Review" }, finish_reason: "stop" }],
          usage: { prompt_tokens: 14, completion_tokens: 5, total_tokens: 19 },
        }));
        return;
      }
      await options.beforeResponse?.(body);
      const selectedTool = options.toolCall?.(body);
      if (selectedTool && tools.some(tool => tool.function?.name === selectedTool.name)) {
        response.end(JSON.stringify({
          id: "chatcmpl-real-core-work-tool", object: "chat.completion", created: 1,
          model: "security-model",
          choices: [{index: 0, message: {role: "assistant", content: null,
            tool_calls: [{id: "call-real-core-work-tool", type: "function", function: {
              name: selectedTool.name, arguments: JSON.stringify(selectedTool.arguments),
            }}]}, finish_reason: "tool_calls"}],
          usage: {prompt_tokens: 24, completion_tokens: 8, total_tokens: 32},
        }));
        return;
      }
      if (options.delayedMessage && options.matchingDelayMs && body.stream !== true && messages.some((message) =>
        typeof message.content === "string" && message.content.includes(options.delayedMessage!))) {
        await new Promise(resolve => setTimeout(resolve, options.matchingDelayMs));
      }
      if (options.goalResponseDelayMs && body.stream !== true && messages.some((message) =>
        typeof message.content === "string" && (message.content.includes("Begin work on the active conversation goal")
          || message.content.includes("Review the active conversation goal")))) {
        await new Promise(resolve => setTimeout(resolve, options.goalResponseDelayMs));
      }
      const responseContent = stub.responseContent;
      if (body.stream === true && options.streamDelayMs !== undefined) {
        response.setHeader("Content-Type", "text/event-stream");
        response.setHeader("Cache-Control", "no-cache");
        response.flushHeaders();
        response.write(`data: ${JSON.stringify({
          id: "chatcmpl-real-core-stream",
          object: "chat.completion.chunk",
          created: 1,
          model: "security-model",
          choices: [{ index: 0, delta: { role: "assistant", content: responseContent ?? "**Core is continuing in Project A**" }, finish_reason: null }],
        })}\n\n`);
        setTimeout(() => {
          if (responseContent === undefined) {
            response.write(`data: ${JSON.stringify({
              id: "chatcmpl-real-core-stream",
              object: "chat.completion.chunk",
              created: 1,
              model: "security-model",
              choices: [{ index: 0, delta: { content: " and finished after the viewer detached." }, finish_reason: null }],
            })}\n\n`);
          }
          response.write(`data: ${JSON.stringify({
            id: "chatcmpl-real-core-stream",
            object: "chat.completion.chunk",
            created: 1,
            model: "security-model",
            choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
            usage: { prompt_tokens: 12, completion_tokens: 10, total_tokens: 22 },
          })}\n\n`);
          response.end("data: [DONE]\n\n");
        }, options.streamDelayMs);
        return;
      }
      response.end(JSON.stringify({
        id: "chatcmpl-real-core",
        object: "chat.completion",
        created: 1,
        model: "security-model",
        choices: [{
          index: 0,
          message: { role: "assistant", content: responseContent ?? "Real Core retained the exact research context." },
          finish_reason: "stop",
        }],
        usage: { prompt_tokens: 18, completion_tokens: 8, total_tokens: 26 },
      }));
      return;
    }
    response.statusCode = 404;
    response.end(JSON.stringify({ error: "not found" }));
  });
  stub.server = server;
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });
  const address = server.address() as AddressInfo;
  stub.origin = `http://127.0.0.1:${address.port}`;
  return stub;
}

export async function stopLocalModelStub(stub: LocalModelStub): Promise<void> {
  stub.server.closeAllConnections();
  await new Promise<void>((resolve, reject) => {
    stub.server.close((error) => error ? reject(error) : resolve());
  });
}
