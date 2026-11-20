const { contextBridge, ipcRenderer } = require("electron");

const TASK_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;

const bridge = Object.freeze({
  getAppInfo: () => ipcRenderer.invoke("crane:app-info"),
  getHealth: () => ipcRenderer.invoke("crane:health"),
  getTasks: () => ipcRenderer.invoke("crane:tasks"),
  getTask: (id) => {
    if (typeof id !== "string" || !TASK_ID_PATTERN.test(id)) {
      return Promise.resolve({
        ok: false,
        error: { message: "Invalid task identifier", kind: "bad_request" },
      });
    }
    return ipcRenderer.invoke("crane:task", id);
  },
});

contextBridge.exposeInMainWorld("craneDesktop", bridge);
