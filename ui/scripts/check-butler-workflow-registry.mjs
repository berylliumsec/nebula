import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { API } from "typescript/unstable/sync";
import { isCallExpression, isIdentifier, isObjectLiteralExpression, isPropertyAccessExpression, isPropertyAssignment } from "typescript/unstable/ast";

const uiRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(uiRoot, "..");
const registryOption = process.argv.indexOf("--registry");
const registryPath = registryOption < 0
  ? resolve(repoRoot, "docs/design/butler-workflow-registry.json")
  : resolve(process.argv[registryOption + 1] ?? "");
if (registryOption >= 0 && !process.argv[registryOption + 1]) throw new Error("--registry requires a path");

function collectCalls() {
  const api = new API();
  try {
    const snapshot = api.updateSnapshot({ openProjects: [resolve(uiRoot, "tsconfig.app.json")] });
    try {
      const project = snapshot.getProjects()[0];
      if (!project) throw new Error("Nebula UI TypeScript project was not loaded");
      const calls = new Map();
      for (const name of project.program.getSourceFileNames()) {
        const path = relative(repoRoot, name).replaceAll("\\", "/");
        if (!path.startsWith("ui/src/") || path.includes(".test.") || path.includes(".spec.") || path === "ui/src/api/client.ts") continue;
        const file = project.program.getSourceFile(name);
        if (!file) throw new Error(`Could not parse ${path}`);
        function visit(node) {
          if (isCallExpression(node) && isPropertyAccessExpression(node.expression)
              && isIdentifier(node.expression.expression) && node.expression.expression.text === "api") {
            const method = node.expression.name.text;
            let key = `${path}::${method}`;
            // A few screens use the generic API client request directly. Keep
            // their route and verb distinct so one existing request cannot
            // hide a new operator action in the same file.
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
          node.forEachChild(visit);
        }
        visit(file);
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
  const catalogs = ["control_assistant_work.py", "butler_projects.py", "butler_chat_export.py", "butler_library.py"]
    .map((name) => readFileSync(resolve(repoRoot, "src/nebula/v3", name), "utf8"))
    .join("\n");
  const ids = new Set();
  const validStatus = new Set(["supported", "gap", "device_handoff"]);
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
    if (action.status === "supported" && action.butler.change.startsWith("missing")) throw new Error(`${action.id}: supported action has no change operation`);
    if (action.status !== "supported" && !action.butler.change.startsWith("missing")) throw new Error(`${action.id}: gap must identify the missing action or handoff`);
    if (action.status === "supported") {
      const [testPath, testName] = action.observed_result_test.split("::");
      const absoluteTest = resolve(repoRoot, testPath ?? "");
      if (!testName || !existsSync(absoluteTest) || !readFileSync(absoluteTest, "utf8").includes(testName)) {
        throw new Error(`${action.id}: observed-result test is missing: ${action.observed_result_test}`);
      }
      for (const operation of Object.values(action.butler).flatMap((value) => value.match(/nebula\.(?:work|projects|chats|library)\.[a-z_]+/g) ?? [])) {
        if (!catalogs.includes(`"${operation}"`)) throw new Error(`${action.id}: Butler operation is not in the Core catalog: ${operation}`);
      }
    }
  }
  const unreviewed = current.filter(({ key }) => !reviewed.has(key)).length;
  console.log(`Butler workflow registry: ${registry.actions.length} reviewed actions, ${reviewed.size}/${current.length} UI API call signatures linked, ${unreviewed} unreviewed signatures`);
}

main();
