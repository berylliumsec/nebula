import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { API } from "typescript/unstable/sync";
import { isCallExpression, isClassDeclaration, isElementAccessExpression, isInterfaceDeclaration, isObjectLiteralExpression, isPropertyAccessExpression, isPropertyAssignment, isStringLiteral } from "typescript/unstable/ast";

const uiRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(uiRoot, "..");
const registryOption = process.argv.indexOf("--registry");
const registryPath = registryOption < 0
  ? resolve(repoRoot, "docs/design/butler-workflow-registry.json")
  : resolve(process.argv[registryOption + 1] ?? "");
if (registryOption >= 0 && !process.argv[registryOption + 1]) throw new Error("--registry requires a path");

// These are protocols used by existing UI owners, never extra model operations.
const transportMethods = new Map([
  ["conversation_protocol", new Set(["cancelChatTurn", "createTemporaryChat", "discardTemporaryChat", "getPendingChatTurn", "keepTemporaryChatAlive", "resumeChatTurn", "stopHarnessTurn", "streamChat", "getControlAssistantSession", "getOrCreateControlAssistantSession", "getHarnessTurn", "getHarnessTurnEvents"])],
  ["editor_completion", new Set(["completeCode"])],
  ["terminal_browser_session", new Set(["containerTerminalCapabilities", "containerTerminalCapacity", "preflightContainerTerminal", "startContainerTerminal", "terminalCommandHistoryStatus"])],
  ["voice_client", new Set(["getVoiceWorkRoster", "getVoiceWorkDetail", "startVoiceSession"])],
  ["authenticated_media", new Set(["fetchChatImagePreview", "uploadChatImage"])],
  ["device_handoff_protocol", new Set(["createHandoff", "cancelHandoff", "consumeHandoff", "resolveHandoff"])],
  ["connection_auth", new Set(["getToken", "health", "setupStatus"])],
  ["request_continuity", new Set(["initializeContinuity", "getContinuityScope", "getPendingRequests", "reconcilePendingRequests", "subscribeContinuity", "dismissCompletedRequest"])],
  ["ui_projection", new Set(["resolveResourceActions", "resolveResource", "listResourceRelations"])],
  ["browser_worker_protocol", new Set(["claimSecurityBrowserCommand", "createSecurityBrowserIntercept", "finishSecurityBrowserCommand", "getEngagementScope", "getSecurityBrowserAutomation", "getSecurityBrowserResearch", "getSecurityBrowserWorkspace", "recordSecurityBrowserAttackResult", "recordSecurityBrowserRepeaterResult", "recordSecurityBrowserSiteNode", "transitionSecurityBrowserAttack", "transitionSecurityBrowserCrawl", "transitionSecurityBrowserRepeaterTab", "uploadEvidence", "uploadSecurityBrowserBodyArtifact", "recordSecurityBrowserTraffic", "recordSecurityBrowserWebSocketFrame"])],
]);

function transportCallIsAllowed(kind, key) {
  const [, method, request] = key.split("::");
  if (kind === "selected_source_preview") {
    return key === 'ui/src/components/ChatAttachments.tsx::request::GET `chat/projects/${encodeURIComponent(projectId)}/sources/${encodeURIComponent(source.id)}/preview`';
  }
  if (kind === "device_read_state") {
    return method === "request" && (/^"PUT" .*\/read-cursor`$/.test(request ?? "") || /^GET .*\/catch-up\?/.test(request ?? ""));
  }
  return transportMethods.get(kind)?.has(method) === true;
}

export function collectCalls(sourceUiRoot = uiRoot) {
  const api = new API();
  try {
    const snapshot = api.updateSnapshot({ openProjects: [resolve(sourceUiRoot, "tsconfig.app.json")] });
    try {
      const project = snapshot.getProjects()[0];
      if (!project) throw new Error("Nebula UI TypeScript project was not loaded");
      // Declaration handles survive aliases and instantiated method symbols.
      // TerminalStopApi is the audited facade passed the real workspace client.
      const owners = [
        {file: "src/api/client.ts", name: "ApiClient", matches: isClassDeclaration},
        {file: "src/components/butlerTerminalStop.ts", name: "TerminalStopApi", matches: isInterfaceDeclaration},
      ];
      const members = [];
      for (const owner of owners) {
        const file = project.program.getSourceFile(resolve(sourceUiRoot, owner.file));
        if (!file) {
          if (owner.name === "ApiClient") throw new Error("ApiClient source was not loaded");
          continue;
        }
        file.forEachChild((node) => {
          if (owner.matches(node) && node.name?.text === owner.name) {
            members.push(...node.members.flatMap((member) => typeof member.name?.text === "string" ? [member.name] : []));
          }
        });
      }
      if (!members.length) throw new Error("ApiClient declarations were not loaded");
      const memberNames = new Set(members.map((member) => member.text));
      const declarationKey = (handle) => JSON.stringify([handle.path, handle.index]);
      const approved = new Set(project.checker.getSymbolAtLocation(members)
        .flatMap((symbol) => symbol?.declarations.map(declarationKey) ?? []));
      const candidates = [];
      for (const name of project.program.getSourceFileNames()) {
        const path = `ui/${relative(sourceUiRoot, name).replaceAll("\\", "/")}`;
        if (!path.startsWith("ui/src/") || path.includes(".test.") || path.includes(".spec.") || path === "ui/src/api/client.ts") continue;
        const file = project.program.getSourceFile(name);
        if (!file) throw new Error(`Could not parse ${path}`);
        function visit(node) {
          if (isCallExpression(node)) {
            const access = node.expression;
            const member = isPropertyAccessExpression(access) ? access.name
              : isElementAccessExpression(access) && isStringLiteral(access.argumentExpression) ? access.argumentExpression
              : undefined;
            if (member && memberNames.has(member.text)) candidates.push({node, member, path, file});
          }
          node.forEachChild(visit);
        }
        visit(file);
      }
      const calls = new Map();
      const symbols = candidates.length ? project.checker.getSymbolAtLocation(candidates.map(({member}) => member)) : [];
      for (let index = 0; index < candidates.length; index += 1) {
        if (!symbols[index]?.declarations.some((handle) => approved.has(declarationKey(handle)))) continue;
        const {node, member, path, file} = candidates[index];
        const method = member.text;
        let key = `${path}::${method}`;
        if (method === "request") {
          const route = node.arguments[0]?.getText(file).replace(/\s+/g, " ") ?? "<missing route>";
          const init = node.arguments[1];
          const methodProperty = init && isObjectLiteralExpression(init)
            ? init.properties.find((property) => isPropertyAssignment(property) && property.name.getText(file) === "method")
            : undefined;
          const verb = methodProperty?.initializer.getText(file) ?? "GET";
          key = `${key}::${verb} ${route}`;
        }
        calls.set(key, (calls.get(key) ?? 0) + 1);
      }
      return [...calls].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([key, count]) => ({ key, count }));
    } finally {
      snapshot.dispose();
    }
  } finally {
    api.close();
  }
}

function main() {
  const registry = JSON.parse(readFileSync(registryPath, "utf8"));
  const current = collectCalls();
  if (process.argv.includes("--write-inventory")) {
    registry.ui_api_calls = current;
    writeFileSync(registryPath, `${JSON.stringify(registry, null, 2)}\n`);
    console.log(`Wrote ${current.length} UI API call signatures`);
    return;
  }
  if (registry.version !== 1 || !Array.isArray(registry.actions) || !Array.isArray(registry.ui_api_calls)) {
    throw new Error("Invalid Butler workflow registry schema");
  }
  const saved = JSON.stringify(registry.ui_api_calls);
  if (JSON.stringify(current) !== saved) {
    const before = new Map(registry.ui_api_calls.map(({ key, count }) => [key, count]));
    const after = new Map(current.map(({ key, count }) => [key, count]));
    const changed = [...new Set([...before.keys(), ...after.keys()])].filter((key) => before.get(key) !== after.get(key));
    throw new Error(`UI API calls changed; review operator workflows and refresh the inventory:\n${changed.slice(0, 30).map((key) => `${key}: ${before.get(key) ?? 0} -> ${after.get(key) ?? 0}`).join("\n")}${changed.length > 30 ? `\n... ${changed.length - 30} more` : ""}`);
  }
  const available = new Set(current.map(({ key }) => key));
  const catalogs = ["settings_assistant_tools.py", "butler_capabilities.py", "control_assistant_work.py", "butler_projects.py", "butler_chat_export.py", "butler_workspace.py", "butler_library.py", "butler_reports.py", "butler_terminal.py", "butler_storage.py", "butler_devices.py", "butler_credentials.py", "butler_providers.py", "butler_harnesses.py", "butler_mcp.py", "butler_notes.py", "butler_records.py", "butler_sources.py", "butler_data.py", "butler_inspection.py", "butler_diagnostics.py"]
    .concat(["butler_administration.py", "memory_tools.py"])
    .map((name) => readFileSync(resolve(repoRoot, "src/nebula/v3", name), "utf8"))
    .join("\n");
  const ids = new Set();
  const validStatus = new Set(["supported", "gap", "device_handoff", "supporting_transport"]);
  const actionsById = new Map(registry.actions.map(action => [action.id, action]));
  const destinations = new Set(JSON.parse(readFileSync(resolve(repoRoot, "src/nebula/v3/assistant_destinations.json"), "utf8")).map(({id}) => id));
  const settingsSource = readFileSync(resolve(repoRoot, "src/nebula/v3/settings_assistant_tools.py"), "utf8");
  const settingsStart = settingsSource.indexOf("SETTING_DESTINATIONS:");
  const settingsBlock = settingsSource.slice(settingsStart, settingsSource.indexOf("\n)\n", settingsStart));
  const settings = new Set([...settingsBlock.matchAll(/\(\s*"([a-z-]+)"\s*,/g)].map((match) => match[1]));
  const reviewed = new Set();
  for (const action of registry.actions) {
    if (!action.id || ids.has(action.id)) throw new Error(`Missing or duplicate workflow ID: ${action.id}`);
    ids.add(action.id);
    for (const field of ["entry_point", "core_owner", "authority", "observed_result_test", "result", "status"]) {
      if (typeof action[field] !== "string" || !action[field].trim()) throw new Error(`${action.id}: ${field} is required`);
    }
    if (!validStatus.has(action.status)) throw new Error(`${action.id}: unknown status ${action.status}`);
    if (!Array.isArray(action.ui_calls) || !action.ui_calls.length) throw new Error(`${action.id}: UI calls are required`);
    for (const key of action.ui_calls) {
      if (!available.has(key)) throw new Error(`${action.id}: missing UI call ${key}`);
      reviewed.add(key);
    }
    if (!action.butler || !["discover", "read", "change"].every((field) => typeof action.butler[field] === "string" && action.butler[field].trim())) {
      throw new Error(`${action.id}: Butler discovery, read, and change disposition are required`);
    }
    if (action.status === "supported" && (action.butler.change.startsWith("missing") || !/\bnebula\.[a-z_]+\.[a-z_]+\b/.test(action.butler.change))) throw new Error(`${action.id}: supported action has no change operation`);
    if (action.status === "gap" && !action.butler.change.startsWith("missing")) throw new Error(`${action.id}: gap must identify the missing action or handoff`);
    if (action.status === "supporting_transport") {
      const transport = action.transport;
      if (!transport || !["core", "requesting_browser"].includes(transport.owner)
          || transport.model_operation !== false
          || typeof transport.reason !== "string" || !transport.reason.trim()
          || !Object.values(action.butler).every(value => value === "not model-callable")) {
        throw new Error(`${action.id}: UI transport must name its owner and explicitly grant no model operation`);
      }
      if (!Array.isArray(transport.workflows) || !transport.workflows.length || transport.workflows.length > 20
          || new Set(transport.workflows).size !== transport.workflows.length
          || !transport.workflows.every(id => ["supported", "device_handoff"].includes(actionsById.get(id)?.status))) {
        throw new Error(`${action.id}: UI transport must reference reviewed operator workflows`);
      }
      if (!action.ui_calls.every(key => transportCallIsAllowed(transport.kind, key))) {
        throw new Error(`${action.id}: this call is not an approved UI transport protocol`);
      }
    } else if (action.transport) {
      throw new Error(`${action.id}: UI transport cannot be marked as a supported mutation or handoff`);
    }
    if (action.status === "device_handoff") {
      const handoff = action.handoff;
      if (!handoff || handoff.owner !== "requesting_browser" || handoff.observed_result !== "opened"
          || !["device_interaction", "device_secret", "operator_authority"].includes(handoff.boundary)
          || typeof handoff.reason !== "string" || !handoff.reason.trim()) {
        throw new Error(`${action.id}: device handoff must name its browser owner, boundary and opened-only result`);
      }
      const navigation = handoff.operation === "nebula.navigate.open";
      if ((!navigation && handoff.operation !== "nebula.settings.open")
          || !(navigation ? destinations : settings).has(handoff.target_id)) {
        throw new Error(`${action.id}: device handoff destination is not in the Core catalog`);
      }
      const argument = navigation ? "destination_id" : "setting_id";
      if (action.butler.change !== `${handoff.operation} ${argument}=${handoff.target_id}`) {
        throw new Error(`${action.id}: device handoff may only open its exact catalog destination`);
      }
    } else if (action.handoff) {
      throw new Error(`${action.id}: browser opening cannot be marked as a supported mutation`);
    }
    if (["supported", "device_handoff", "supporting_transport"].includes(action.status)) {
      const [testPath, testName] = action.observed_result_test.split("::");
      const absoluteTest = resolve(repoRoot, testPath ?? "");
      if (!testName || !existsSync(absoluteTest) || !readFileSync(absoluteTest, "utf8").includes(testName)) {
        throw new Error(`${action.id}: observed-result test is missing: ${action.observed_result_test}`);
      }
      for (const operation of Object.values(action.butler).flatMap((value) => value.match(/nebula\.(?:work|projects|chats|workspace|library|reports|terminal|storage|devices|credentials|providers|harnesses|mcp|memory|notes|assets|findings|sources|data|inspect|settings|diagnostics|navigate)\.[a-z_]+/g) ?? [])) {
        if (!catalogs.includes(`"${operation}"`)) throw new Error(`${action.id}: Butler operation is not in the Core catalog: ${operation}`);
      }
    }
  }
  const unreviewed = current.filter(({ key }) => !reviewed.has(key)).length;
  const gaps = registry.actions.filter(({status}) => status === "gap").length;
  const handoffs = registry.actions.filter(({status}) => status === "device_handoff").length;
  const transports = registry.actions.filter(({status}) => status === "supporting_transport").length;
  console.log(`Butler workflow registry: ${registry.actions.length} reviewed actions, ${reviewed.size}/${current.length} UI API call signatures linked, ${unreviewed} unreviewed signatures, ${gaps} gaps, ${handoffs} operator-owned device handoffs, ${transports} supporting UI protocols (no model grants)`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
