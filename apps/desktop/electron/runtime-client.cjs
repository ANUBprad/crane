const { execFile } = require("node:child_process");
const { existsSync } = require("node:fs");
const path = require("node:path");

const { promisify } = require("node:util");

const execFileAsync = promisify(execFile);

const API_BASE = "http://127.0.0.1:8787";
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const TASK_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;
const REQUEST_TIMEOUT_MS = 5000;
const TOKEN_TIMEOUT_MS = 5000;
const MAX_RESPONSE_BYTES = 1 << 20;
const MAX_TASK_INPUT_BYTES = 64 << 10;

let cachedToken = null;
let pendingToken = null;

class RuntimeError extends Error {
  constructor(message, kind = "error") {
    super(message);
    this.name = "RuntimeError";
    this.kind = kind;
  }
}

function resolveAgentBinary() {
  const executable = process.platform === "win32" ? "agent.exe" : "agent";
  const candidates = [];
  if (process.env.CRANE_AGENT_BINARY) {
    candidates.push(path.resolve(process.env.CRANE_AGENT_BINARY));
  }
  if (process.resourcesPath) {
    candidates.push(path.join(process.resourcesPath, executable));
  }
  candidates.push(path.join(__dirname, "../../../", executable));
  candidates.push(path.join(process.cwd(), executable));
  return candidates.find((candidate) => existsSync(candidate)) || null;
}

async function readToken() {
  if (cachedToken) return cachedToken;
  if (pendingToken) return pendingToken;

  pendingToken = (async () => {
    const binary = resolveAgentBinary();
    if (!binary) {
      throw new RuntimeError("Go runtime executable was not found", "disconnected");
    }
    try {
      const { stdout } = await execFileAsync(binary, ["api-token"], {
        timeout: TOKEN_TIMEOUT_MS,
        windowsHide: true,
        maxBuffer: 64 * 1024,
      });
      const token = String(stdout).trim();
      if (!TOKEN_PATTERN.test(token)) {
        throw new RuntimeError("Go runtime returned an invalid installation token", "error");
      }
      cachedToken = token;
      return token;
    } catch (error) {
      if (error instanceof RuntimeError) throw error;
      const kind = error && (error.code === "ENOENT" || error.code === "ETIMEDOUT")
        ? "disconnected"
        : "error";
      // Never include stdout, stderr, arguments or paths in the error text.
      throw new RuntimeError("Unable to read the protected installation token", kind);
    }
  })();

  try {
    return await pendingToken;
  } finally {
    pendingToken = null;
  }
}

function validateTaskID(id) {
  if (typeof id !== "string" || !TASK_ID_PATTERN.test(id)) {
    throw new RuntimeError("Invalid task identifier", "bad_request");
  }
}

function errorKind(status, code) {
  if (status === 401) return "unauthorized";
  if (status === 404 || code === "not_found") return "not_found";
  if (status === 409 || code === "task_already_exists") return "conflict";
  if (status === 400 || code === "bad_request") return "bad_request";
  if (status === 503 && code === "task_control_unavailable") return "disconnected";
  return "error";
}

async function readJSON(response) {
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new RuntimeError("Supervisor returned an invalid JSON response", "error");
  }
  if (!response.ok) {
    const code = payload && payload.error && typeof payload.error.code === "string"
      ? payload.error.code
      : "request_failed";
    throw new RuntimeError(`Supervisor request failed (${code})`, errorKind(response.status, code));
  }
  if (!payload || typeof payload !== "object" || !payload.data || typeof payload.data !== "object") {
    throw new RuntimeError("Supervisor returned an unexpected response", "error");
  }
  return payload.data;
}

async function withTokenRetry(request, retried = false) {
  const token = await readToken();
  let response;
  try {
    response = await request(token);
  } catch (error) {
    if (error && error.name === "AbortError") throw error;
    throw new RuntimeError("Local supervisor is unreachable", "disconnected");
  }
  if (response.status === 401 && !retried) {
    cachedToken = null;
    return withTokenRetry(request, true);
  }
  return response;
}

async function apiGet(endpoint) {
  const response = await withTokenRetry((token) => fetch(`${API_BASE}${endpoint}`, {
    method: "GET",
    headers: { Authorization: `Bearer ${token}` },
    redirect: "manual",
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  }));

  const contentLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(contentLength) && contentLength > MAX_RESPONSE_BYTES) {
    if (response.body) void response.body.cancel();
    throw new RuntimeError("Supervisor response was too large", "error");
  }
  return readJSON(response);
}

async function apiPost(endpoint, body) {
  let encoded;
  if (body !== undefined) {
    encoded = JSON.stringify(body);
    if (Buffer.byteLength(encoded, "utf8") > MAX_RESPONSE_BYTES) {
      throw new RuntimeError("Request body was too large", "bad_request");
    }
  }
  const buildOptions = (token) => {
    const headers = { Authorization: `Bearer ${token}` };
    const options = {
      method: "POST",
      headers,
      redirect: "manual",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    };
    if (encoded !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = encoded;
    }
    return options;
  };
  const response = await withTokenRetry((token) => fetch(`${API_BASE}${endpoint}`, buildOptions(token)));
  return readJSON(response);
}

function parseSSEBlock(block) {
  const event = { id: "", event: "", data: "" };
  for (const line of block.split("\n")) {
    const trimmed = line.trimEnd();
    if (trimmed.startsWith("id: ")) event.id = trimmed.slice(4).trim();
    else if (trimmed.startsWith("event: ")) event.event = trimmed.slice(7).trim();
    else if (trimmed.startsWith("data: ")) event.data = trimmed.slice(6).trim();
  }
  if (!event.event || !event.data) return null;
  try {
    event.data = JSON.parse(event.data);
  } catch {
    return null;
  }
  return event;
}

async function subscribeTaskEvents(id, cursor, onEvent, onError) {
  validateTaskID(id);
  if (typeof onEvent !== "function") {
    throw new RuntimeError("Event callback is required", "bad_request");
  }
  const query = Number.isSafeInteger(cursor) && cursor > 0 ? `?cursor=${cursor}` : "";
  const controller = new AbortController();
  let response;
  try {
    response = await withTokenRetry((token) => fetch(
      `${API_BASE}/v1/tasks/${encodeURIComponent(id)}/events${query}`,
      {
        method: "GET",
        headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
        redirect: "manual",
        signal: controller.signal,
      },
    ));
  } catch (error) {
    if (error.name !== "AbortError") throw error;
    return { abort() {} };
  }

  if (!response.ok) {
    let code = "request_failed";
    try {
      const payload = await response.json();
      if (payload && payload.error && typeof payload.error.code === "string") code = payload.error.code;
    } catch {
      // Keep the generic code when the error body is not JSON.
    }
    throw new RuntimeError(`Event stream failed (${code})`, errorKind(response.status, code));
  }
  if (!response.body) {
    throw new RuntimeError("Supervisor did not open an event stream", "error");
  }

  void (async () => {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let boundary = buffer.indexOf("\n\n");
        while (boundary >= 0) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const event = parseSSEBlock(block);
          if (event) onEvent(event);
          boundary = buffer.indexOf("\n\n");
        }
      }
      if (!controller.signal.aborted && typeof onError === "function") {
        onError(new RuntimeError("Event stream closed by the supervisor", "disconnected"));
      }
    } catch (error) {
      if (controller.signal.aborted || (error && error.name === "AbortError")) return;
      if (typeof onError === "function") {
        onError(error instanceof RuntimeError
          ? error
          : new RuntimeError("Event stream interrupted", "disconnected"));
      }
    }
  })();

  return { abort: () => controller.abort() };
}

async function getHealth() {
  const data = await apiGet("/v1/health");
  if (typeof data.ready !== "boolean" || typeof data.supervisor !== "boolean" ||
      typeof data.persistence !== "boolean" || !data.runtime ||
      typeof data.runtime.available !== "boolean") {
    throw new RuntimeError("Supervisor returned an incomplete health record", "error");
  }
  return data;
}

async function getTasks() {
  const data = await apiGet("/v1/tasks");
  if (!Array.isArray(data.tasks)) {
    throw new RuntimeError("Supervisor returned an invalid task list", "error");
  }
  return data;
}

async function getTask(id) {
  validateTaskID(id);
  const data = await apiGet(`/v1/tasks/${encodeURIComponent(id)}`);
  if (!data.task || typeof data.task !== "object") {
    throw new RuntimeError("Supervisor returned an invalid task detail", "error");
  }
  return data.task;
}

async function submitTask(id, input) {
  validateTaskID(id);
  if (typeof input !== "string" || !input.trim()) {
    throw new RuntimeError("Task input is required", "bad_request");
  }
  if (Buffer.byteLength(input, "utf8") > MAX_TASK_INPUT_BYTES) {
    throw new RuntimeError("Task input exceeds the 64 KiB limit", "bad_request");
  }
  const data = await apiPost("/v1/tasks", { id, input });
  if (!data.task || typeof data.task !== "object") {
    throw new RuntimeError("Supervisor returned an invalid admitted task", "error");
  }
  return data.task;
}

async function cancelTask(id) {
  validateTaskID(id);
  const data = await apiPost(`/v1/tasks/${encodeURIComponent(id)}/cancel`);
  if (!data.task || typeof data.task !== "object") {
    throw new RuntimeError("Supervisor returned an invalid cancellation result", "error");
  }
  return data.task;
}

module.exports = { getHealth, getTasks, getTask, submitTask, cancelTask, subscribeTaskEvents };
