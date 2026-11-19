const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("craneDesktop", Object.freeze({
  getAppInfo: () => ipcRenderer.invoke("crane:app-info"),
}));