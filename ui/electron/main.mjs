import { app, BrowserWindow, ipcMain } from "electron";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { BrowserHost } from "./browser-host.mjs";
import { companionOperation } from "./companion.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const uiRoot = resolve(here, "..");
const repoRoot = resolve(uiRoot, "..");
const bootstrapProtocol = "nebula-sidecar-v1";
const environmentKeys = new Set([
  "HOME", "USERPROFILE", "PATH", "LANG", "LC_ALL", "TZ", "DISPLAY", "WAYLAND_DISPLAY",
  "XAUTHORITY", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "PYENV_ROOT", "PYENV_VERSION",
  "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
  "OPENROUTER_API_KEY", "ORCAROUTER_API_KEY", "DEEPSEEK_API_KEY", "XAI_API_KEY",
  "HF_TOKEN", "DOCKER_HOST", "CONTAINER_HOST", "NEBULA_PLAYWRIGHT_EXECUTABLE",
  "PLAYWRIGHT_BROWSERS_PATH", "NEBULA_MITMDUMP_EXECUTABLE", "NEBULA_SECURITY_PROXY_ADDON",
]);

function dataRoot() {
  if (process.platform === "darwin") return join(homedir(), "Library", "Application Support", "io.berylliumsec.nebula");
  if (process.platform === "win32") return join(process.env.APPDATA || app.getPath("appData"), "io.berylliumsec.nebula");
  return join(process.env.XDG_DATA_HOME || join(homedir(), ".local", "share"), "io.berylliumsec.nebula");
}

async function deviceId() {
  const file = join(dataRoot(), "electron-device.json");
  try {
    const value = JSON.parse(await readFile(file, "utf8"));
    if (typeof value.deviceId === "string" && /^desktop-[a-zA-Z0-9_-]{12,}$/.test(value.deviceId)) return value.deviceId;
  } catch { /* First launch creates the identity below. */ }
  const id = `desktop-${randomBytes(18).toString("base64url")}`;
  await mkdir(dataRoot(), { recursive: true, mode: 0o700 });
  await writeFile(file, JSON.stringify({ deviceId: id }), { mode: 0o600, flag: "wx" });
  return id;
}

function errorResponse(response, status, detail) {
  response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  response.end(JSON.stringify({ detail }));
}

async function jsonBody(request) {
  let body = "";
  for await (const chunk of request) {
    body += chunk;
    if (body.length > 5_600_000) throw new Error("Browser request exceeds its size limit.");
  }
  return JSON.parse(body || "{}");
}

async function startBrowserBridge(getHost) {
  const token = randomBytes(32).toString("base64url");
  const server = createServer((request, response) => {
    void (async () => {
      const supplied = request.headers.authorization?.replace(/^Bearer /, "") || "";
      const valid = supplied.length === token.length &&
        timingSafeEqual(Buffer.from(supplied), Buffer.from(token));
      if (!valid || request.method !== "POST") return errorResponse(response, 401, "Browser bridge denied the request.");
      const body = await jsonBody(request);
      const host = getHost();
      if (!host) return errorResponse(response, 503, "The desktop browser is disconnected.");
      let result;
      if (request.url === "/v1/identities/ensure") {
        if (typeof body.identity_id !== "string" || typeof body.project_id !== "string") {
          return errorResponse(response, 400, "Browser identity and Project are required.");
        }
        host.attachIdentity(body.identity_id, body.project_id);
        result = { ready: true };
      } else {
        const match = /^\/v1\/companion\/([a-zA-Z0-9_-]{1,200})$/.exec(request.url || "");
        if (!match) return errorResponse(response, 404, "Unknown browser bridge operation.");
        result = await companionOperation(host, match[1], body);
      }
      response.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      response.end(JSON.stringify(result));
    })().catch((error) => errorResponse(response, 409, error instanceof Error ? error.message : "Browser operation failed."));
  });
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  return { server, token, url: `http://127.0.0.1:${server.address().port}` };
}

function boundedHandshake(child) {
  return new Promise((resolveHandshake, reject) => {
    let buffer = "";
    const timer = setTimeout(() => reject(new Error("Core did not complete its desktop handshake in 60 seconds.")), 60_000);
    const fail = (error) => { clearTimeout(timer); reject(error); };
    child.once("error", fail);
    child.once("exit", (code) => fail(new Error(`Core exited before handshake (${code}).`)));
    child.stdout.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      if (buffer.length > 16_384) return fail(new Error("Core handshake exceeded its size limit."));
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      clearTimeout(timer);
      child.stdout.removeAllListeners("data");
      child.stdout.resume();
      try {
        const value = JSON.parse(buffer.slice(0, newline));
        if (value.protocol !== bootstrapProtocol || value.host !== "127.0.0.1" ||
            !Number.isInteger(value.port) || value.port < 1 || value.port > 65535) {
          throw new Error("Core returned an invalid desktop handshake.");
        }
        resolveHandshake(value);
      } catch (error) { reject(error); }
    });
  });
}

async function startCore(bridge) {
  const bundled = app.isPackaged;
  const staticDir = bundled ? join(process.resourcesPath, "ui-dist") : join(uiRoot, "dist");
  const devPython = bundled ? null : process.env.NEBULA_DESKTOP_PYTHON;
  if (devPython && !isAbsolute(devPython)) {
    throw new Error("NEBULA_DESKTOP_PYTHON must be an absolute path to the selected Python interpreter.");
  }
  const executable = bundled
    ? join(dirname(process.execPath), process.platform === "win32" ? "nebula-core.exe" : "nebula-core")
    : devPython || "poetry";
  const serveArgs = ["serve", "--host", "127.0.0.1", "--port", "0", "--handshake-stdout", "--data-dir", join(dataRoot(), "core"), "--static-dir", staticDir];
  const args = bundled
    ? serveArgs
    : devPython ? ["-m", "nebula.v3.cli", ...serveArgs] : ["-C", repoRoot, "run", "nebula-core", ...serveArgs];
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => environmentKeys.has(key)));
  if (!bundled) env.PYTHONPATH = join(repoRoot, "src");
  env.NEBULA_ONE_BROWSER_CUTOVER = "1";
  env.NEBULA_VISIBLE_BROWSER_URL = bridge.url;
  env.NEBULA_VISIBLE_BROWSER_TOKEN = bridge.token;
  if (!bundled && !env.NEBULA_SECURITY_PROXY_ADDON) {
    env.NEBULA_SECURITY_PROXY_ADDON = join(repoRoot, "security-proxy", "addon.py");
  }
  const child = spawn(executable, args, { cwd: repoRoot, env, stdio: ["pipe", "pipe", "pipe"],
    detached: process.platform !== "win32", windowsHide: true });
  const token = randomBytes(32).toString("base64url");
  child.stdin.write(JSON.stringify({ protocol: bootstrapProtocol, ipc_token: token }) + "\n");
  child.stderr.on("data", (bytes) => process.stderr.write(bytes));
  const handshake = await boundedHandshake(child);
  const endpoint = `http://127.0.0.1:${handshake.port}/api/v1`;
  for (let attempt = 0; attempt < 600; attempt++) {
    if (child.exitCode !== null) throw new Error("Core stopped before becoming ready.");
    try {
      const response = await fetch(`${endpoint}/health`, { headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(1500) });
      const health = await response.json();
      if (response.ok && health.status === "ok" && health.database === "ok") {
        return { child, endpoint, token };
      }
    } catch { /* Core is still starting. */ }
    await new Promise((done) => setTimeout(done, 100));
  }
  throw new Error("Core did not become healthy in 60 seconds.");
}

let window;
let host;
let bridge;
let core;

function requireShell(event) {
  if (!window || event.sender !== window.webContents || window.isDestroyed()) {
    throw new Error("Browser controls are available only in the Nebula desktop shell.");
  }
}

function handle(channel, callback) {
  ipcMain.handle(channel, (event, payload) => { requireShell(event); return callback(payload); });
}

app.whenReady().then(async () => {
  if (!app.requestSingleInstanceLock()) { app.quit(); return; }
  window = new BrowserWindow({ width: 1440, height: 900, minWidth: 720, minHeight: 500,
    backgroundColor: "#0c141a", show: false,
    webPreferences: { preload: join(here, "preload.cjs"), sandbox: true,
      contextIsolation: true, nodeIntegration: false, webSecurity: true } });
  const coreConnection = { endpoint: "", token: "" };
  host = new BrowserHost(window, coreConnection);
  bridge = await startBrowserBridge(() => host);
  core = await startCore(bridge);
  coreConnection.endpoint = core.endpoint;
  coreConnection.token = core.token;

  handle("nebula:core-connection", () => ({ ...coreConnection,
    protocol: bootstrapProtocol, source: "local" }));
  handle("nebula:device-id", () => deviceId());
  handle("nebula:set-zoom", (scale) => {
    if (typeof scale !== "number" || scale < 0.5 || scale > 2) throw new Error("Unsupported zoom.");
    window.webContents.setZoomFactor(scale);
    host.setLayout(null);
    return scale;
  });
  handle("nebula:browser-open", (projectId) => host.open(projectId));
  handle("nebula:browser-new-tab", ({ projectId, url }) => host.newTab(projectId, url));
  handle("nebula:browser-select-tab", ({ projectId, tabId }) => host.selectTab(projectId, tabId));
  handle("nebula:browser-close-tab", ({ projectId, tabId }) => host.closeTab(projectId, tabId));
  handle("nebula:browser-navigate", ({ projectId, tabId, url }) => host.navigate(projectId, tabId, url));
  handle("nebula:browser-command", ({ projectId, tabId, action }) => host.command(projectId, tabId, action));
  handle("nebula:browser-layout", (layout) => host.setLayout(layout));

  if (process.env.NEBULA_ELECTRON_DEV_URL) await window.loadURL(process.env.NEBULA_ELECTRON_DEV_URL);
  else await window.loadURL(core.endpoint.replace(/\/api\/v1$/, "/"));
  window.show();
}).catch((error) => { process.stderr.write(`Nebula Electron startup failed: ${error?.stack || error}\n`); app.quit(); });

app.on("before-quit", () => {
  host?.close();
  bridge?.server.close();
  if (core?.child?.pid) {
    try {
      if (process.platform === "win32") core.child.kill();
      else process.kill(-core.child.pid, "SIGTERM");
    } catch { /* Core may already have stopped. */ }
  }
});
