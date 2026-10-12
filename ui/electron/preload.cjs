const { contextBridge, ipcRenderer } = require("electron");

const invoke = (name, payload) => ipcRenderer.invoke(name, payload);

contextBridge.exposeInMainWorld("nebulaDesktop", Object.freeze({
  kind: "electron",
  resolveBackendConnection: () => invoke("nebula:core-connection"),
  deviceId: () => invoke("nebula:device-id"),
  setZoom: (scale) => invoke("nebula:set-zoom", scale),
  browser: Object.freeze({
    open: (projectId) => invoke("nebula:browser-open", projectId),
    newTab: (projectId, url) => invoke("nebula:browser-new-tab", { projectId, url }),
    selectTab: (projectId, tabId) => invoke("nebula:browser-select-tab", { projectId, tabId }),
    closeTab: (projectId, tabId) => invoke("nebula:browser-close-tab", { projectId, tabId }),
    navigate: (projectId, tabId, url) => invoke("nebula:browser-navigate", { projectId, tabId, url }),
    command: (projectId, tabId, action) => invoke("nebula:browser-command", { projectId, tabId, action }),
    layout: (value) => invoke("nebula:browser-layout", value),
    onState: (callback) => {
      const listener = (_event, value) => callback(value);
      ipcRenderer.on("nebula:browser-state", listener);
      return () => ipcRenderer.removeListener("nebula:browser-state", listener);
    },
  }),
}));
