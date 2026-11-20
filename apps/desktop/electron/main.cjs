const { app, BrowserWindow, ipcMain, shell } = require("electron");
const path = require("node:path");
const runtime = require("./runtime-client.cjs");

const isDevelopment = !app.isPackaged;

function createWindow() {
  const window = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1080,
    minHeight: 700,
    backgroundColor: "#090b0f",
    title: "CRANE",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });

  window.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://")) void shell.openExternal(url);
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    const allowed = isDevelopment
      ? url.startsWith("http://127.0.0.1:5173")
      : url.startsWith("file://");
    if (!allowed) event.preventDefault();
  });

  if (isDevelopment) {
    void window.loadURL("http://127.0.0.1:5173");
  } else {
    void window.loadFile(path.join(__dirname, "../dist/index.html"));
  }
}

async function invoke(action) {
  try {
    return { ok: true, data: await action() };
  } catch (error) {
    return {
      ok: false,
      error: {
        message: error instanceof Error ? error.message : "Runtime request failed",
        kind: error && error.kind ? error.kind : "error",
      },
    };
  }
}

ipcMain.handle("crane:app-info", () => ({
  name: "CRANE",
  version: app.getVersion(),
  platform: process.platform,
}));
ipcMain.handle("crane:health", () => invoke(() => runtime.getHealth()));
ipcMain.handle("crane:tasks", () => invoke(() => runtime.getTasks()));
ipcMain.handle("crane:task", (_event, id) => invoke(() => runtime.getTask(id)));

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
