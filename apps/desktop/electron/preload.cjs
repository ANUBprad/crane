const { contextBridge, ipcRenderer } = require("electron");

const TASK_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;
const STREAM_ID_PATTERN = /^stream-\d+$/;
const MAX_INPUT_BYTES = 64 << 10;

function invalid(message) {
  return Promise.resolve({
    ok: false,
    error: { message, kind: "bad_request" },
  });
}

const bridge = Object.freeze({
  getAppInfo: () => ipcRenderer.invoke("crane:app-info"),
  getHealth: () => ipcRenderer.invoke("crane:health"),
  getTasks: () => ipcRenderer.invoke("crane:tasks"),
  getTask: (id) => {
    if (typeof id !== "string" || !TASK_ID_PATTERN.test(id)) {
      return invalid("Invalid task identifier");
    }
    return ipcRenderer.invoke("crane:task", id);
  },
  submitTask: (id, input) => {
    if (typeof id !== "string" || !TASK_ID_PATTERN.test(id)) {
      return invalid("Invalid task identifier");
    }
    if (typeof input !== "string" || !input.trim()) {
      return invalid("Task input is required");
    }
    if (new TextEncoder().encode(input).length > MAX_INPUT_BYTES) {
      return invalid("Task input exceeds the 64 KiB limit");
    }
    return ipcRenderer.invoke("crane:task-submit", id, input);
  },
  cancelTask: (id) => {
    if (typeof id !== "string" || !TASK_ID_PATTERN.test(id)) {
      return invalid("Invalid task identifier");
    }
    return ipcRenderer.invoke("crane:task-cancel", id);
  },
  subscribeTaskEvents: async (id, cursor, onEvent, onError) => {
    if (typeof id !== "string" || !TASK_ID_PATTERN.test(id)) {
      return invalid("Invalid task identifier");
    }
    if (typeof onEvent !== "function") {
      return invalid("Event callback is required");
    }
    const result = await ipcRenderer.invoke("crane:task-events-subscribe", id, cursor);
    if (!result.ok) return result;

    const streamId = result.data && result.data.streamId;
    if (typeof streamId !== "string" || !STREAM_ID_PATTERN.test(streamId)) {
      return invalid("Supervisor returned an invalid event stream");
    }
    const eventChannel = `crane:task-event:${streamId}`;
    const errorChannel = `crane:task-event-error:${streamId}`;
    const eventListener = (_event, payload) => onEvent(payload);
    const errorListener = (_event, error) => {
      if (typeof onError === "function") onError(error);
    };
    ipcRenderer.on(eventChannel, eventListener);
    ipcRenderer.on(errorChannel, errorListener);

    let unsubscribed = false;
    return {
      ok: true,
      data: {
        streamId,
        unsubscribe: () => {
          if (unsubscribed) return;
          unsubscribed = true;
          ipcRenderer.removeListener(eventChannel, eventListener);
          ipcRenderer.removeListener(errorChannel, errorListener);
          void ipcRenderer.invoke("crane:task-events-unsubscribe", streamId);
        },
      },
    };
  },
});

contextBridge.exposeInMainWorld("craneDesktop", bridge);
