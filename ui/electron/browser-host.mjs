import { createHash, randomUUID } from "node:crypto";
import { dialog, session, WebContentsView } from "electron";
import { pageBounds } from "./geometry.mjs";

function networkURL(value) {
  const url = new URL(value);
  if (!["http:", "https:"].includes(url.protocol) || !url.hostname ||
      url.username || url.password) throw new Error("Open an HTTP(S) address without embedded credentials.");
  return url.href;
}

export class BrowserHost {
  constructor(window, core) {
    this.window = window;
    this.core = core;
    this.projects = new Map();
    this.identityProjects = new Map();
    this.activeProject = null;
    this.layout = null;
    window.on("resize", () => { this.layout = null; this.place(); });
    window.on("closed", () => this.close());
  }

  emit() {
    if (!this.window.isDestroyed()) this.window.webContents.send("nebula:browser-state", this.state());
  }

  state(projectId = this.activeProject) {
    const project = this.projects.get(projectId);
    return {
      projectId,
      activeTabId: project?.activeTabId ?? null,
      tabs: project ? [...project.tabs.values()].map(({ id, view, loading, error }) => ({
        id, url: view.webContents.getURL() || "about:blank",
        title: view.webContents.getTitle() || "New tab", loading, error,
      })) : [],
    };
  }

  async authorize(projectId, value) {
    const url = networkURL(value);
    const response = await fetch(
      `${this.core.endpoint}/engagements/${encodeURIComponent(projectId)}/browser-navigation-check`,
      { method: "POST", headers: { Authorization: `Bearer ${this.core.token}`,
        "Content-Type": "application/json" }, body: JSON.stringify({ url }), signal: AbortSignal.timeout(5000) },
    );
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(typeof body.detail === "string" ? body.detail : "Project scope refused this address.");
    }
    return url;
  }

  project(projectId) {
    const project = this.projects.get(projectId);
    if (!project) throw new Error("Open the Project browser before using its tabs.");
    return project;
  }

  tab(projectId, tabId) {
    const project = this.project(projectId);
    const tab = project.tabs.get(tabId || project.activeTabId);
    if (!tab) throw new Error("The selected browser tab is closed.");
    return tab;
  }

  async open(projectId) {
    if (typeof projectId !== "string" || !/^[a-zA-Z0-9_-]{1,200}$/.test(projectId)) {
      throw new Error("Select a valid Project before opening its browser.");
    }
    if (!this.projects.has(projectId)) {
      const partition = `persist:nebula-visible-${createHash("sha256").update(projectId).digest("hex")}`;
      const ses = session.fromPartition(partition);
      ses.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
      ses.setPermissionCheckHandler(() => false);
      ses.on("will-download", async (_event, item) => {
        item.pause();
        const { canceled, filePath } = await dialog.showSaveDialog(this.window, {
          defaultPath: item.getFilename(), title: "Save browser download",
        });
        if (canceled || !filePath) item.cancel();
        else { item.setSavePath(filePath); item.resume(); }
      });
      ses.webRequest.onBeforeRequest({ urls: ["http://*/*", "https://*/*"] }, (details, callback) => {
        if (details.resourceType !== "mainFrame") return callback({});
        const owned = [...(this.projects.get(projectId)?.tabs.values() ?? [])]
          .some((tab) => tab.view.webContents.id === details.webContentsId);
        if (!owned) return callback({ cancel: true });
        void this.authorize(projectId, details.url)
          .then(() => callback({})).catch(() => callback({ cancel: true }));
      });
      this.projects.set(projectId, { partition, tabs: new Map(), activeTabId: null });
      await this.newTab(projectId);
    }
    this.activeProject = projectId;
    this.place();
    this.emit();
    return this.state();
  }

  async newTab(projectId, url = null) {
    const project = this.project(projectId);
    if (project.tabs.size >= 16) throw new Error("Close a tab before opening another.");
    const id = randomUUID();
    const view = new WebContentsView({ webPreferences: {
      partition: project.partition, sandbox: true, contextIsolation: true,
      nodeIntegration: false, webSecurity: true, allowRunningInsecureContent: false,
    } });
    const tab = { id, view, loading: false, error: null };
    project.tabs.set(id, tab);
    project.activeTabId = id;
    const contents = view.webContents;
    contents.setWindowOpenHandler(({ url: popup }) => {
      setImmediate(() => { void this.newTab(projectId, popup).catch((error) => {
        tab.error = error.message; this.emit();
      }); });
      return { action: "deny" };
    });
    contents.on("will-navigate", (event, destination) => {
      try { networkURL(destination); }
      catch { event.preventDefault(); tab.error = "Only HTTP and HTTPS pages can open in Browser."; this.emit(); }
    });
    contents.on("did-start-loading", () => { tab.loading = true; this.emit(); });
    contents.on("did-stop-loading", () => { tab.loading = false; this.emit(); });
    contents.on("page-title-updated", () => this.emit());
    contents.on("did-navigate", () => this.emit());
    contents.on("did-navigate-in-page", () => this.emit());
    contents.on("did-fail-load", (_event, code, description, attempted, mainFrame) => {
      if (mainFrame && code !== -3) { tab.error = `${description} (${code})`; this.emit(); }
    });
    if (url) await this.navigate(projectId, id, url);
    this.place(); this.emit();
    return this.state();
  }

  selectTab(projectId, tabId) {
    const project = this.project(projectId);
    this.tab(projectId, tabId);
    project.activeTabId = tabId;
    this.activeProject = projectId;
    this.place(); this.emit();
    return this.state();
  }

  closeTab(projectId, tabId) {
    const project = this.project(projectId);
    const tab = this.tab(projectId, tabId);
    if (this.window.contentView.children.includes(tab.view)) this.window.contentView.removeChildView(tab.view);
    tab.view.webContents.close();
    project.tabs.delete(tab.id);
    if (project.activeTabId === tab.id) project.activeTabId = project.tabs.keys().next().value ?? null;
    this.place(); this.emit();
    if (!project.activeTabId) return this.newTab(projectId);
    return this.state();
  }

  async navigate(projectId, tabId, value) {
    const tab = this.tab(projectId, tabId);
    const url = await this.authorize(projectId, value);
    tab.error = null;
    await tab.view.webContents.loadURL(url);
    this.emit();
    return this.state();
  }

  async command(projectId, tabId, action) {
    const contents = this.tab(projectId, tabId).view.webContents;
    if (action === "back" && contents.canGoBack()) contents.goBack();
    else if (action === "forward" && contents.canGoForward()) contents.goForward();
    else if (action === "reload") contents.reload();
    else if (!["back", "forward", "reload"].includes(action)) throw new Error("Unknown browser command.");
    return this.state();
  }

  setLayout(layout) {
    this.layout = layout;
    this.place();
  }

  place() {
    if (this.window.isDestroyed()) return;
    const contentBounds = this.window.getContentBounds();
    const bounds = this.layout && pageBounds(this.layout, contentBounds);
    if (process.env.NEBULA_BROWSER_GEOMETRY === "1") {
      const message = JSON.stringify({ layout: this.layout, contentBounds, bounds });
      if (message !== this.lastGeometryLog) {
        process.stderr.write(`electron browser geometry: ${message}\n`);
        this.lastGeometryLog = message;
      }
    }
    for (const [projectId, project] of this.projects) {
      for (const tab of project.tabs.values()) {
        const selected = bounds && projectId === this.activeProject && tab.id === project.activeTabId;
        const mounted = this.window.contentView.children.includes(tab.view);
        if (!mounted) this.window.contentView.addChildView(tab.view);
        if (selected) {
          tab.view.setBounds(bounds);
          tab.view.setVisible(true);
        } else tab.view.setVisible(false);
      }
    }
  }

  attachIdentity(identityId, projectId) {
    if (!this.projects.has(projectId)) throw new Error("The desktop browser for this Project is disconnected.");
    const existing = this.identityProjects.get(identityId);
    if (existing && existing !== projectId) throw new Error("Browser identity belongs to another Project.");
    this.identityProjects.set(identityId, projectId);
  }

  identityProject(identityId) {
    const projectId = this.identityProjects.get(identityId);
    if (!projectId || !this.projects.has(projectId)) throw new Error("The attached desktop browser is disconnected.");
    return projectId;
  }

  close() {
    for (const project of this.projects.values()) for (const tab of project.tabs.values()) {
      if (!tab.view.webContents.isDestroyed()) tab.view.webContents.close();
    }
    this.projects.clear(); this.identityProjects.clear();
  }
}
